"""Spec review: sub-agents that examine the ISASpec, in parallel.

Gate 1, link 1 and Gate 2 all establish that an instruction *means what its
spec says*. The evaluation in ``eval/`` showed that is not the same as the spec
being good: a fully verified extension came out 2% slower than the baseline at
+74% area, because nothing in the loop asked whether the instructions could be
built for the target's coprocessor interface, whether the application could call
them without marshalling operands, whether they returned the values callers
need, or whether they were faster at all.

Those four questions are now four reviewers, one per prompt in
``red/prompts/review/``. Each is a stateless turn — the spec, the hot-loop
report and the platform's measured cycle costs go in as text, a JSON array of
findings comes back — so they carry no MCP tool servers and all four dispatch
concurrently, costing one model round trip instead of four. Blocking findings go
back to the designer through the sealed status tool (see ``red.loop``).
"""

from __future__ import annotations

import json
import os
import re

from chia.base.ChiaFunction import get

from red import state_def
from red.constants import LLM_PROMPTS_DIR, LLM_RESOURCE, REVIEW_MAX_FINDINGS

REVIEWERS = ("implementability", "callability", "benefit", "legality")


def _prior_digest(prior, reviewer: str) -> str:
    """What this reviewer said last round, so the next round adjudicates rather
    than redraws. Independent re-readings never converge: each one is free to
    invent a new objection, and the designer never learns which of its changes
    landed."""
    if prior is None or not prior.findings:
        return ("This is the first review of this spec. Judge it on its own "
                "terms.")
    mine = [f for f in prior.findings if f.reviewer == reviewer]
    if not mine:
        return ("You raised nothing last round. Other reviewers did; the spec "
                "may have changed in response.")
    out = ["You raised these last round. Say for each whether the current spec "
           "resolves it — repeat a finding verbatim if it still stands, and drop "
           "it entirely if it does not.", ""]
    for f in mine:
        out.append(f"- [{f.severity}] {f.instruction or 'spec-wide'}: {f.finding}")
    return "\n".join(out)


def _others_digest(prior, reviewer: str) -> str:
    """The *other* reviewers' blocking findings from last round.

    Reviewers judge independently, which is the point — but nothing showed them
    where they contradicted each other, and a contradiction is unresolvable by
    the designer. One run had implementability block a 16-word operand block as
    too large for a 750-LUT core while benefit asked for exactly that block for
    its arithmetic intensity; the designer had to overrule a reviewer, and the
    round it spent doing so regressed the spec. Each reviewer now sees the
    conflicting claims and must say which authority settles them, instead of
    restating its own.
    """
    if prior is None or not prior.findings:
        return "(nothing — this is the first review.)"
    theirs = [f for f in prior.findings
              if f.reviewer != reviewer and f.severity == "blocking"]
    if not theirs:
        return "(no other reviewer raised a blocking finding last round.)"
    out = ["Another reviewer raised these as blocking last round:", ""]
    for f in theirs:
        out.append(f"- **{f.reviewer}** on {f.instruction or 'spec-wide'}: "
                   f"{f.finding}")
    out += ["",
            "If one of your findings contradicts one of these — you want an "
            "operand block the other calls unbuildable, or vice versa — do not "
            "simply restate yours. Say which authority settles it and defer to "
            "it: the **CoreProfile above** is authoritative on what this core "
            "can hold and what its interface can do, and the **performance "
            "gate** is authoritative on whether the extension is fast enough. "
            "The designer cannot satisfy two reviewers who want opposite "
            "things, and a round spent trying is a round lost."]
    return "\n".join(out)


def _charter(reviewer: str, spec_json: str, report_json: str, core: str,
             core_profile=None, prior=None, perf=None, security=None) -> str:
    """The full prompt for one reviewer: the shared frame plus its question."""
    common = open(os.path.join(LLM_PROMPTS_DIR, "review", "_common.md")).read()
    question = open(os.path.join(LLM_PROMPTS_DIR, "review", f"{reviewer}.md")).read()
    return (common
            .replace("${SECURITY}", render_security(security))
            .replace("${QUESTION}", question.strip())
            .replace("${CORE}", os.path.basename(core.rstrip("/")) or core)
            .replace("${CORE_PROFILE}", render_core(core_profile))
            .replace("${PRIOR}", _prior_digest(prior, reviewer))
            .replace("${OTHERS}", _others_digest(prior, reviewer))
            .replace("${PERF}", render_perf(perf))
            .replace("${REPORT}", _report_digest(report_json))
            .replace("${SPEC}", spec_json))


def render_perf(perf) -> str:
    """Gate 3's verdict, as the reviewers see it.

    Without this a reviewer re-opens a question the loop has already measured
    and passed — "use a wider operand block so it goes faster" against a spec
    already predicted at 3.29x. Gate 3 prices every instruction on its measured
    work, so whether the extension is *fast enough* is settled before the
    reviewers ever see it; what is not settled is whether it can be built,
    called, and is worth its area.
    """
    if perf is None:
        return ("The performance gate has not run on this spec, so treat its "
                "speed as unestablished.")
    lines = [f"The performance gate **passed** this spec at "
             f"**{perf.app_speedup:.2f}x**" if perf.meets_target() else
             f"The performance gate **failed** this spec at "
             f"{perf.app_speedup:.2f}x",
             f"over {perf.covered_share:.0%} of profiled cycles, pricing every "
             f"instruction on the work its C model measurably performs:", "",
             "```", perf.detail, "```", ""]
    if perf.meets_target():
        lines.append(
            "So **whether the extension is fast enough is already settled**, by "
            "measurement. Do not raise a blocking finding that amounts to "
            "\"make it faster\" or \"use a wider operand block for more "
            "throughput\" — that question is closed. An instruction the gate "
            "credits with *no* benefit at all, or one whose area is out of "
            "proportion to what the gate credits it, is still worth raising.")
    return "\n".join(lines)


def render_security(report) -> str:
    """Gate 4's verdict, as the reviewers see it.

    The same reason ``render_perf`` exists: a reviewer that cannot see what has
    already been established re-opens it, and the designer spends a round
    answering a question a sanitizer already answered. It is also the honest
    place to say what was *not* checked, so a reviewer knows where its own
    judgement is the only thing watching.
    """
    if report is None:
        return ("The security gate has not run on this spec, so treat its "
                "memory safety and side-channel behaviour as unestablished.")
    hard = report.mechanical_blocking()
    lines = [
        f"Gate 4 ran {len(report.checks_run)} mechanical checks on every C "
        f"model over {report.vectors:,} adversarial operand values each, plus a "
        "security sub-agent that probes the models with values chosen from the "
        "arithmetic's corner cases.", ""]
    lines.append(f"- **{len(hard)} mechanically confirmed blocking finding(s)**"
                 if hard else
                 "- **No mechanically confirmed security defect** — memory "
                 "safety, output completeness, purity, constant-time execution "
                 "and decode legality are settled for this spec.")
    for f in hard:
        lines.append(f"  - `{f.instruction or 'spec-wide'}` [{f.check}]: {f.finding}")
    advisory = [f for f in report.findings
                if f.severity in ("blocking", "major") and not f.is_mechanical()]
    if advisory:
        lines.append("- advisory (not established mechanically):")
        for f in advisory[:6]:
            lines.append(f"  - `{f.instruction or 'spec-wide'}`: {f.finding}")
    if report.checks_skipped:
        lines += ["", "Checks that could NOT run on the host (so nothing is "
                  "watching these):"]
        lines += [f"  - {s}" for s in report.checks_skipped[:6]]
    lines += ["", "Do not raise a blocking finding that restates one of the "
              "above, and do not raise memory safety or constant-time "
              "execution as blocking unless you can name an input the gate "
              "missed — say so as `major` instead, and say which input."]
    return "\n".join(lines)


def render_core(profile) -> str:
    """The CoreProfile as an agent reads it. Without one, say so plainly rather
    than letting a reviewer assume a machine that may not be this one."""
    if profile is None:
        return ("(The target core has not been analysed. Assume only a small "
                "in-order 32-bit RISC-V and do not rely on details you cannot "
                "see.)")
    out = [f"- core: {profile.name} ({profile.isa})",
           f"- microarchitecture: {profile.microarchitecture}",
           f"- coprocessor: {profile.coprocessor} — "
           f"{'CAN' if profile.coprocessor_memory_access else 'CANNOT'} address "
           f"memory itself; result: {profile.coprocessor_result}",
           f"- memory: {profile.memory_interface}"]
    if profile.existing_units:
        out.append("- functional units already present (do not duplicate): "
                   + ", ".join(profile.existing_units))
    if profile.custom_opcode_space:
        out.append("- free custom opcodes: " + ", ".join(profile.custom_opcode_space))
    if profile.register_file:
        out.append(f"- register file: {profile.register_file}")
    costs = [f"load {profile.cycles_per_load:g}",
             f"ALU op {profile.cycles_per_alu_op:g}",
             f"coprocessor issue {profile.coprocessor_issue_overhead:g}",
             f"word moved {profile.cycles_per_word_moved:g}"]
    out.append("- cycles: " + ", ".join(c for c in costs if not c.endswith(" 0")))
    if profile.area_note:
        out.append(f"- area: {profile.area_note}")
    for c in profile.constraints:
        out.append(f"- CONSTRAINT: {c}")
    return "\n".join(out)


def _report_digest(report_json: str) -> str:
    """The hot loops as a reviewer needs them: what is hot, how hot, and the
    body text. Sending the whole report would bury that in cycle counts."""
    try:
        rep = state_def.from_json(report_json, state_def.HotLoopReport)
    except (json.JSONDecodeError, TypeError, ValueError):
        return report_json[:4000]
    out = [f"workload {rep.workload}, coverage {rep.coverage:.1%} "
           f"(profiled with {rep.profile_method})"]
    for loop in rep.ranked():
        out.append(f"\n### {loop.loop_id} — {loop.cycle_share:.1%} of cycles"
                   f"  [{loop.source_file}]")
        if loop.operand_stats:
            out.append(f"operands: {loop.operand_stats}")
        if loop.body:
            body = loop.body.strip().splitlines()
            out.append("```c\n" + "\n".join(body[:40]) + "\n```")
    return "\n".join(out)


def _parse_findings(reviewer: str, text: str) -> list[state_def.ReviewFinding]:
    """Pull the JSON array out of a reviewer's reply.

    Models wrap JSON in prose or fences however they like, so take the first
    bracketed array that parses and ignore the rest. A reviewer that returns
    nothing usable is recorded as having found nothing rather than failing the
    round — the gates, not the reviewers, decide whether a spec ships."""
    if not text:
        return []
    blob = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", blob, re.S)
    if fence:
        blob = fence.group(1).strip()
    start = blob.find("[")
    if start < 0:
        return []
    for end in range(len(blob), start, -1):
        if blob[end - 1] != "]":
            continue
        try:
            items = json.loads(blob[start:end])
        except json.JSONDecodeError:
            continue
        if not isinstance(items, list):
            return []
        out = []
        for it in items[:REVIEW_MAX_FINDINGS]:
            if not isinstance(it, dict):
                continue
            sev = str(it.get("severity", "minor")).lower()
            out.append(state_def.ReviewFinding(
                reviewer=reviewer,
                instruction=str(it.get("instruction", "") or ""),
                severity=sev if sev in ("blocking", "major", "minor") else "minor",
                finding=str(it.get("finding", "") or "")[:600],
                evidence=str(it.get("evidence", "") or "")[:800],
                fix=str(it.get("fix", "") or "")[:600]))
        return out
    return []


def review_spec(llm, spec_json: str, report_json: str, core: str,
                dispatch, reviewers=REVIEWERS, core_profile=None, prior=None,
                perf=None, security=None) -> state_def.ReviewReport:
    """Run every reviewer concurrently and merge what they found.

    *dispatch* is a callable ``(message, tag) -> ObjectRef`` supplied by the
    loop, so this module never has to know how the LLM is scheduled. All the
    refs are created before any is resolved — that is what makes the reviewers
    parallel rather than four turns in a row.
    """
    if llm is None:
        return state_def.ReviewReport(reviewers=[], findings=[])

    refs = [dispatch(_charter(r, spec_json, report_json, core, core_profile,
                              prior, perf, security),
                     f"review:{r}")
            for r in reviewers]
    findings: list[state_def.ReviewFinding] = []
    ran: list[str] = []
    for reviewer, ref in zip(reviewers, refs):
        if ref is None:
            continue
        try:
            res = get(ref)
        except Exception as exc:                      # noqa: BLE001
            print(f"[red] reviewer {reviewer} failed: {exc}")
            continue
        ran.append(reviewer)
        if res is not None and not getattr(res, "success", True):
            print(f"[red] reviewer {reviewer}: backend reported failure")
            continue
        findings.extend(_parse_findings(reviewer, getattr(res, "result", "") or ""))

    findings = _dedupe(findings)
    order = {"blocking": 0, "major": 1, "minor": 2}
    findings.sort(key=lambda f: order.get(f.severity, 3))
    return state_def.ReviewReport(reviewers=ran, findings=findings)


_NONWORD = re.compile(r"[^a-z0-9 ]+")
# Words that carry no signal about *which* defect is being described.
_NOISE = frozenset("""
the this that these those with without which what where when will would should
could must have has been being from into onto over under about across instead
rather than then them they their there here also only just even more most less
than because since while during before after both each every some any all none
instruction instructions spec design model value values field fields
""".split())
# How much two findings must overlap to be the same finding.
_SAME = float(os.environ.get("RED_REVIEW_DEDUPE", "0.45"))


def _terms(f) -> set:
    """The content words of a claim, crudely stemmed.

    Stemming matters more than it looks: four reviewers describing one defect
    wrote "collides", "collide", "encodings" and "funct7" about the same
    encoding clash, and an exact match on word sets treats those as four
    different problems — which is exactly the failure this function exists to
    stop.
    """
    out = set()
    for w in _NONWORD.sub(" ", (f.finding or "").lower()).split():
        if len(w) <= 3 or w in _NOISE:
            continue
        for suffix in ("ing", "es", "ed", "s"):
            if len(w) > 5 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


def _dedupe(findings: list) -> list:
    """Collapse one objection raised by several reviewers into one finding.

    All four reviewers read the same spec, so a real defect is usually found by
    all four — and the loop then counted it four times, showed it to the
    designer four times, and ranked a spec with one defect as though it had
    four. Merging keeps the part that is genuinely stronger evidence (how many
    reviewers independently agreed) and drops the repetition.

    Similarity rather than equality, because reviewers paraphrase. The
    threshold is deliberately high enough that two different objections about
    the same instruction stay separate.
    """
    order = {"blocking": 0, "major": 1, "minor": 2}
    kept: list = []
    for f in findings:
        terms = _terms(f)
        for group in kept:
            seen, seen_terms = group
            if (seen.instruction or "") != (f.instruction or ""):
                continue
            union = terms | seen_terms
            if not union:
                continue
            if len(terms & seen_terms) / len(union) < _SAME:
                continue
            # Same defect. Keep the worst severity, the fullest evidence, and
            # record that another reviewer reached it independently.
            if order.get(f.severity, 3) < order.get(seen.severity, 3):
                seen.severity = f.severity
                seen.finding = f.finding
            who = {r.strip() for r in seen.reviewer.split("+")} | {f.reviewer}
            seen.reviewer = "+".join(sorted(who))
            if len(f.evidence or "") > len(seen.evidence or ""):
                seen.evidence = f.evidence
            if not seen.fix and f.fix:
                seen.fix = f.fix
            # The group now stands for every wording of this defect, so later
            # findings are compared against the union. Without this, a third
            # reviewer's phrasing is matched only against the first one that
            # happened to arrive, and near-misses pile up as separate rows.
            group[1] = seen_terms | terms
            break
        else:
            kept.append([f, terms])
    return [f for f, _ in kept]


def render(report: state_def.ReviewReport) -> str:
    """The review as the designer reads it out of the status tool."""
    if not report.findings:
        return ("# Spec review\n\nReviewers: " + ", ".join(report.reviewers) +
                "\n\nNo findings — every reviewer passed the spec.\n")
    lines = ["# Spec review", "",
             f"Reviewers: {', '.join(report.reviewers)}",
             f"Findings: {report.blocking()} blocking, "
             f"{sum(1 for f in report.findings if f.severity == 'major')} major, "
             f"{sum(1 for f in report.findings if f.severity == 'minor')} minor", ""]
    for f in report.findings:
        who = f"`{f.instruction}`" if f.instruction else "spec-wide"
        lines += [f"## [{f.severity.upper()}] {who} — {f.reviewer}",
                  f"**Finding.** {f.finding}"]
        if f.evidence:
            lines.append(f"**Evidence.** {f.evidence}")
        if f.fix:
            lines.append(f"**Suggested fix.** {f.fix}")
        lines.append("")
    return "\n".join(lines)
