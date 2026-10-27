"""RED loop driver — orchestrate A1 kernel mining + A2 ISA synthesis.

A1 and A2 are the research contribution and RED's whole scope; the **ISASpec is
the final artifact**. Each stage is *agentic* (a Claude Code turn over the
sealed MCP tools -- see :mod:`red.backend`) and bounded by a *programmatic*
edge from :mod:`red.nodes`:

    A1  profile the project (mechanical) x2  ->  Gate 1 (±5% re-profile)
        mining agent ranks hot loops         ->  HotLoopReport
    A2  synthesis agent (S2a->S2b->S2c)      ->  ISASpec
        link1 (coverage + C models built and stress-run)
    review  four sub-agents examine the spec concurrently -> ReviewReport
        blocking findings go back to the designer, which revises (capped)
    ISS implementer agent (a separate agent, from the spec's words alone)
        gate2 / link2 (C model <=> patched Spike, 10^5 vectors)

The gates ask whether an instruction *means what its spec says*. The reviewers
ask the questions the gates cannot — can it be built for this core, can the
application call it, does it return what callers need, is it faster — because
eval/ showed a fully verified spec can still be slower than the code it
replaces. Their findings are the loop's feedback edge back to the designer.

Every artifact is persisted as typed YAML (HotLoopReport / ISASpec) and
mirrored to the durable DB sweep as it is produced.

Everything runs on one machine. The mechanical edges are ordinary function
calls; each agent turn is a ``claude -p`` subprocess given only the sealed MCP
tools that stage is entitled to (:mod:`red.backend`), and the tool servers are
uvicorn apps on loopback ports in threads of this process
(:mod:`red.localtool`).

Two run modes:
    default        the whole loop
    --mechanical   the mechanical edges only — profile, Gate 1, the typed
                   artifacts — with no agents and no model spend
"""

from __future__ import annotations

import argparse
import datetime
import io
import os
import re
import sys
import tarfile
import uuid
from dataclasses import dataclass, field

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import time

import concurrent.futures as cf

from red import (cost, db_node, graph, modelcfg, nodes, review, rsi, security,
                 state_def, structure, workload as workload_mod)
from red.constants import (
    COVERAGE_TARGET,
    CRITIC_MAX_ROUNDS,
    core_source_roots,
    DB_ROOT,
    EXAMPLE_CFLAGS,
    EXAMPLE_CORE,
    EXAMPLE_HARNESS,
    EXAMPLE_PROJECT,
    EXAMPLE_WORKLOAD,
    GATE2_VECTORS,
    LINK1_VECTORS,
    AGENT_EFFORT,
    AGENT_MAX_PARALLEL,
    AGENT_MODEL,
    LLM_PROMPTS_DIR,
    LLM_RATE_LIMIT_BACKOFF,
    LLM_RATE_LIMIT_RETRIES,
    RED_LOG_ROOT,
    REPAIR_MAX_ROUNDS,
    PERF_MAX_ROUNDS,
    REVIEW_MAX_ROUNDS,
    SECURITY_MAX_PROBES,
    SECURITY_MAX_ROUNDS,
    SPEEDUP_TARGET,
    WORKLOAD_MAX_ROUNDS,
    WORKLOAD_MIN_IR,
    WORKLOAD_MIN_PROJECT_SHARE,
    WORKLOAD_DETERMINISM,
    find_sources,
)
from red.backend import QueryResult, make_router
from red.localtool import quiet_servers
from red.tools import DesignerTool, IssTool, SecurityTool, WorkloadTool



@dataclass
class REDResult:
    """End-to-end RED loop outcome the driver summarizes."""

    workload: str
    converged: bool
    iterations: int
    gate0: state_def.GateResult | None = None
    gate1: state_def.GateResult | None = None
    gate2: state_def.GateResult | None = None
    gate3: state_def.GateResult | None = None
    gate4: state_def.GateResult | None = None
    chain: list[state_def.GateResult] = field(default_factory=list)
    report: state_def.HotLoopReport | None = None
    spec: state_def.ISASpec | None = None
    review: state_def.ReviewReport | None = None
    security: state_def.SecurityReport | None = None
    workload_spec: state_def.WorkloadSpec | None = None
    model: str = ""


# ---------------------------------------------------------------------------
# Prompt / artifact helpers
# ---------------------------------------------------------------------------

def _load_prompt(name: str, **subs: str) -> str:
    text = open(os.path.join(LLM_PROMPTS_DIR, name)).read()
    for key, val in subs.items():
        text = text.replace("${" + key + "}", val)
    return text


def _tool_map(tools) -> dict[str, str]:
    """{PLACEHOLDER: full tool-call name} for prompt substitution.

    Each MCP method is registered as ``<tool>_<method>`` (the ``name=`` passed
    to ``mcp.add_tool`` in red/tools.py), and Claude Code namespaces it by the
    server it came from, so the agent calls it as
    ``mcp__<tool>__<tool>_<method>``. The charters are written with
    placeholders and filled in here, so a renamed tool renames itself in every
    prompt.

    The method list comes from each tool's own ``METHODS`` — the same tuple it
    registers with MCP — and never from a copy kept here. A second list drifts:
    a method added to a tool but missing from this one substitutes as the empty
    string, and the charter then tells the agent to call a tool with no name.
    That is a silent failure, and it is the same shape as the bug the charter
    placeholder test exists to catch.
    """
    names = {}
    for t in tools:
        for fn in getattr(t, "METHODS", ()):
            names[fn] = f"{t.name}__{t.name}_{fn}"[:64]
    return names


def _review_rank(report, link1_result, perf=None, bar=None) -> tuple:
    """How good a reviewed spec is, lowest is best.

    Blocking findings first — they are what convergence is defined by — then
    whether link 1 still passes, then whether the design still clears the
    performance bar, then total findings. Nothing here is anything the designer
    asserts about its own spec; every term is a gate's or a reviewer's verdict.

    Performance is in the tuple because leaving it out let the review destroy a
    design and be rewarded for it: on matrixmul the reviewers took a draft Gate
    3 priced at 14.57x — the number the RTL independently measured — down to
    three blocking findings, and the revision that cleared them priced at
    1.03x. Both revisions are "0 blocking" as far as the old ranking could see,
    so it kept the one that had given the performance away. Answering a reviewer
    is not worth having if the extension stops being worth building.
    """
    fast_enough = 0
    if perf is not None and bar is not None:
        fast_enough = 0 if perf.meets_target(bar) else 1
    return (report.blocking(),
            0 if (link1_result is not None and link1_result.passed) else 1,
            fast_enough,
            len(report.findings))


# What the backend says when it is asking us to slow down rather than telling
# us the request was wrong. Matched loosely and across several phrasings on
# purpose: the cost of a false positive is one wasted backoff, and the cost of
# a false negative is abandoning a design that was never refused.
_THROTTLED = re.compile(r"rate.?limit|\b429\b|quota exceeded|usage limit|"
                        r"overloaded|too many requests|try again later", re.I)

# Agent turns are subprocesses waiting on the network, so they overlap well and
# cost this process almost nothing while they do. One pool for the whole run.
_POOL = cf.ThreadPoolExecutor(max_workers=AGENT_MAX_PARALLEL,
                              thread_name_prefix="red-agent")


def _model_tag(llm) -> str:
    """How the run should name the thing that produced it.

    The effort level changes the output as much as the model does, and under a
    routing file different stages run on different models, so neither "opus"
    nor one effort level describes a run on its own. Name the default and say
    where the rest came from; the full table is in the run log and the file is
    on disk.
    """
    cfg = getattr(llm, "models", None)
    if cfg is None:
        return ""
    label = cfg.fallback.label()
    routed = [r for r in cfg.routed.values() if r != cfg.fallback]
    if cfg.source and routed:
        extra = ""
        others = [b for b in cfg.backends if b != cfg.fallback.backend]
        if others:
            extra = f", also {'/'.join(others)}"
        return (f"{label} + {len(routed)} stage overrides{extra} "
                f"({cfg.source})")
    return label


def _dispatch_llm(llm, message: str, tools, tag: str = "agent",
                  log_path: str | None = None, role: str = ""):
    """Run one agent turn, synchronously. None-safe (used by --mechanical runs).

    A failed turn comes back as ``QueryResult(success=False)`` rather than an
    exception, because every stage above has a bounded repair edge and those
    only work if a failure is a value they can act on.

    A throttled turn is not a failed one. Treating the two alike cost three
    runs in one batch: two consecutive rate limits tripped the "backend is
    down" guard, the run abandoned synthesis, and the report said a design
    could not be produced when what actually happened is that we were asked to
    wait.
    """
    if llm is None:
        return None
    res = None
    _started = time.monotonic()
    for attempt in range(LLM_RATE_LIMIT_RETRIES + 1):
        try:
            res = llm.prompt(message, tools, role=role)
        except Exception as exc:                              # noqa: BLE001
            res = QueryResult(returncode=-1, success=False,
                              stderr=f"{type(exc).__name__}: {exc}")
        # A usage limit can arrive either as the CLI's own error or as the
        # body of an errored result, so look at both.
        throttled = bool(res) and bool(_THROTTLED.search(
            (res.stderr or "") + ("" if res.success else " " + (res.result or ""))))
        if not throttled or attempt == LLM_RATE_LIMIT_RETRIES:
            break
        wait = LLM_RATE_LIMIT_BACKOFF * (2 ** attempt)
        print(f"[red] {tag} turn throttled (attempt {attempt + 1}/"
              f"{LLM_RATE_LIMIT_RETRIES + 1}) — waiting {wait:.0f}s")
        time.sleep(wait)
    _record(res, tag, log_path)
    # Every agent turn in the run passes through here, so this is the one
    # place that can say what a run cost. red.rsi reads it to decide whether
    # a change to the prompts made the next run cheaper.
    rsi.record_turn(tag=tag, role=role, model=_model_tag(llm),
                    seconds=time.monotonic() - _started,
                    ok=bool(res and res.success),
                    reply_chars=len((res.result or "") if res else ""))
    return res


def _dispatch_async(llm, message: str, tag: str = "agent", tools=(),
                    role: str = ""):
    """Start an agent turn without waiting for it.

    Used wherever turns are independent: the four reviewers (toolless — their
    context is the prompt and their answer is JSON), and A1 mining alongside A0
    core analysis, which read different things. The caller starts every turn
    first and collects them afterwards, so they genuinely overlap.

    Returns a Future, or None when there is no backend.
    """
    if llm is None:
        return None
    return _POOL.submit(_dispatch_llm, llm, message, tools, tag, None, role)


def _resolve(fut, tag: str, log_path: str | None = None):
    """Collect a turn started by :func:`_dispatch_async`."""
    if fut is None:
        return None
    try:
        res = fut.result()
    except Exception as exc:                                  # noqa: BLE001
        print(f"[red] {tag} turn RAISED {type(exc).__name__}: {str(exc)[:200]}")
        return QueryResult(returncode=-1, stderr=str(exc), success=False)
    _record(res, tag, log_path)
    return res


def _record(res, tag: str, log_path: str | None) -> None:
    """Report a failed turn, and keep every turn's transcript with the run."""
    if res is None:
        return
    if not getattr(res, "success", True):
        print(f"[red] WARNING: {tag} turn FAILED "
              f"(rc={getattr(res, 'returncode', '?')}): "
              f"{(getattr(res, 'stderr', '') or '')[:200]}")
    if log_path and getattr(res, "stream_result", ""):
        with open(log_path, "a") as f:
            f.write(f"\n{'=' * 78}\n### {tag}\n{'=' * 78}\n{res.stream_result}")


def _publish_status(status_path: str, gate: state_def.GateResult,
                    label: str = "") -> None:
    """Put a verdict where the agents can read it (the sealed StatusTool).

    Ground truth flows to the agents through a file the loop writes and they can
    only read — never through something an agent could assert about itself."""
    with open(status_path, "w") as f:
        f.write(f"# Verification status{' — ' + label if label else ''}\n\n"
                f"- {gate.gate}: {'PASS' if gate.passed else 'FAIL'} — {gate.detail}\n\n"
                f"## Counterexample\n```\n{gate.counterexample or '(none)'}\n```\n")


def _dispatch_iss(llm, iss_tools, log_path: str, tag: str):
    """One ISS-implementer turn.

    It runs against its own tool set (:class:`IssSpecTool` hides the C models
    and accepts only ``spike_model``) and with the A2 system charter cleared, so
    nothing from the designer's turn leaks into the implementation that is
    supposed to cross-check it."""
    itm = _tool_map(iss_tools)
    message = _load_prompt(
        "iss.md",
        READ_SPEC=itm.get("read_spec", ""), WRITE_SPEC=itm.get("write_spec", ""),
        READ_STATUS=itm.get("read_status", ""),
        READ_KNOWLEDGE=itm.get("read_knowledge", ""))
    previous, llm.system_message = llm.system_message, ""
    try:
        return _dispatch_llm(llm, message, iss_tools, tag=tag, role="iss",
                             log_path=log_path)
    finally:
        llm.system_message = previous


def _parse_core_profile(core: str, res) -> state_def.CoreProfile | None:
    """Turn the A0 analyst's reply into a CoreProfile.

    Falls back to a profile carrying only the core's name when the reply cannot
    be parsed or fails validation: the loop must still run for a core nobody has
    analysed, it just runs without the architectural guidance."""
    name = os.path.basename(core.rstrip("/")) or core
    text = (getattr(res, "result", "") or "").strip()
    blob = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if blob:
        text = blob.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            profile = state_def.from_yaml(text[start:end + 1], state_def.CoreProfile)
            profile.name = profile.name or name
            state_def.validate_core_profile(profile)
            return profile
        except (TypeError, ValueError) as exc:
            print(f"[red] A0: unusable core profile ({exc}); "
                  "the designer will work without it")
    return None


def _read_artifact(path: str, cls, fallback=None):
    if os.path.exists(path):
        try:
            return state_def.from_yaml(open(path).read(), cls)
        except (TypeError, ValueError):
            pass
    return fallback


def _read_findings(path: str) -> list:
    """The security agent's findings, as the sealed tool left them on disk.

    Its probe results and its reported findings both land here, already typed
    and already capped in severity by ``red.security`` — this only has to read
    them back, and to survive a turn that wrote nothing at all."""
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            items = state_def.loads(f.read())
    except (ValueError, OSError):
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            try:
                out.append(state_def.SecurityFinding(**it))
            except TypeError:
                continue
    return out


def _spec_digest(spec: state_def.ISASpec) -> str:
    """The spec as a reviewer needs it at a glance: what each instruction is,
    how wide its operands are, and what it claims to do. The full text, C models
    included, is one tool call away — pasting it here as well would bury the
    shape of the extension in code."""
    if spec is None:
        return "(no spec)"
    lines = [f"**{spec.name}** {spec.version} — {spec.description}", ""]
    for ins in spec.instructions:
        lines += [f"### `{ins.mnemonic}` ({ins.opcode}, {ins.words} words in / "
                  f"{ins.words} words out, {ins.mac_ops} multiplies)",
                  f"- replaces: {ins.replaces} x{ins.invocations} per call",
                  f"- semantics: {ins.semantics}",
                  "```", (ins.pseudocode or "").strip()[:1500], "```", ""]
    return "\n".join(lines)


def _write_artifact(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(state_def.to_yaml(obj))


# ---------------------------------------------------------------------------
# A1 — kernel mining
# ---------------------------------------------------------------------------

_BODY_PLACEHOLDER = re.compile(
    r"not readable|unavailable|see source|n/?a\b|^\s*/\*[^*]*\*/\s*$|^\s*$",
    re.I)


def _function_source(roots, function: str, limit: int = 6000) -> str:
    """The text of *function* as it appears in the project's own source.

    A1 is supposed to quote the loop it mined, and when it cannot read the file
    it writes what it believes instead -- one run recorded the body as
    ``/* Not readable due to path escape error */`` and the designer then wrote
    C models for a kernel it had never seen. The body is *evidence*, so the loop
    supplies it mechanically and does not depend on an agent having managed to
    open a file.

    Definition-finding is deliberately crude: the first line that begins a
    definition of this name, then brace matching. A declaration or a call is
    skipped because it has no ``{`` before its ``;``.
    """
    if not function:
        return ""
    pat = re.compile(r"(^|[^\w])" + re.escape(function) + r"\s*\(")
    found: list = []
    for root in roots:
        for _rel, full in find_sources(root, exts=(".c", ".h", ".inc", ".inl")):
            try:
                with open(full, errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for m in pat.finditer(text):
                start = text.rfind("\n", 0, m.start()) + 1
                # A definition reaches a '{' before any ';'.
                brace = text.find("{", m.end())
                semi = text.find(";", m.end())
                if brace < 0 or (0 <= semi < brace):
                    continue
                depth, i = 0, brace
                while i < len(text):
                    if text[i] == "{":
                        depth += 1
                    elif text[i] == "}":
                        depth -= 1
                        if depth == 0:
                            line = text.count("\n", 0, start) + 1
                            found.append((os.path.basename(full), line,
                                          text[start:i + 1]))
                            break
                    i += 1
    if not found:
        return ""
    # A project may carry hand-written assembly variants of the same routine
    # for other ISAs (micro-ecc ships ARM and AVR ``asm_*.inc`` versions of
    # uECC_vli_mult). They are not the semantics to model for a RISC-V target,
    # so drop them when a C definition exists.
    c_defs = [f for f in found if not os.path.basename(f[0]).startswith("asm_")]
    if c_defs:
        found = c_defs
    if len(found) == 1:
        body = found[0][2]
        return (body[:limit] + "\n/* ... truncated */"
                if len(body) > limit else body)
    # Several definitions of one name: micro-ecc defines
    # vli_mmod_fast_secp256r1 three times, once per uECC_WORD_SIZE, and only
    # the build's macros decide which is live. Show them all, labelled, rather
    # than silently picking the first and handing the designer the 8-bit
    # variant of a 32-bit kernel.
    each = max(limit // len(found), 800)
    out = [f"/* {len(found)} definitions of {function}() — the build selects "
           f"one by macro; match it to the workload's word size. */"]
    for fname, line, body in found:
        out.append(f"\n/* ---- {fname}:{line} ---- */\n"
                   + (body[:each] + "\n/* ... truncated */"
                      if len(body) > each else body))
    return "\n".join(out)


def build_mechanical_report(project: str, workload: str,
                            profile: dict) -> state_def.HotLoopReport:
    """Fallback HotLoopReport straight from the profile: rank every profiled
    function by cycle/instruction count, hottest first, until the tail. Used
    when the mining agent has not (or cannot) write its own selection."""
    counts = {k: v for k, v in (profile.get("cycle_counts") or {}).items()
              if k != "__total__" and v > 0}
    total = sum(counts.values()) or 0
    loops = []
    for i, (fn, n) in enumerate(sorted(counts.items(), key=lambda kv: -kv[1])):
        share = n / total if total else 0.0
        if loops and share < 0.01:      # stop at the long tail
            break
        loops.append(state_def.HotLoop(
            loop_id=f"{fn}::hot", function=fn, source_file="", body="", ir="",
            trip_count=0, dynamic_cycles=n, cycle_share=share,
            operand_stats={}, rank=i + 1))
    return state_def.HotLoopReport(
        workload=workload,
        profile_method=profile.get("profile_method", "none"),
        loops=loops,
        coverage=min(1.0, sum(l.cycle_share for l in loops)),
        profile_runs=[total],
        profile_command=profile.get("command", ""))


def _merge_mechanical(report: state_def.HotLoopReport,
                      profile: dict,
                      source_roots=()) -> state_def.HotLoopReport:
    """Overwrite the report's dynamic numbers from the mechanical profile — the
    agent selects/ranks loops; the loop supplies cycle share, coverage, runs."""
    counts = profile.get("cycle_counts") or {}
    calls = profile.get("call_counts") or {}
    total = sum(v for k, v in counts.items() if k != "__total__") or 0
    for loop in report.loops:
        fn = loop.function or loop.loop_id.split("::")[0]
        n = counts.get(fn, loop.dynamic_cycles)
        loop.dynamic_cycles = n
        loop.cycle_share = (n / total) if total else loop.cycle_share
        # Cost per *call* is what Gate 3 compares an instruction against, and
        # only the profiler knows how often a kernel actually runs.
        loop.calls = calls.get(fn, loop.calls)
        # The mined loop's own source, from the file rather than from the
        # agent's recollection of it. Everything downstream -- the designer's C
        # model, the reviewers' semantics checks -- reads this field.
        if source_roots and _BODY_PLACEHOLDER.search(loop.body or ""):
            real = _function_source(source_roots, fn)
            if real:
                loop.body = real
    # Rank is mechanical too: hottest first, by measured share.
    for i, loop in enumerate(sorted(report.loops, key=lambda l: -l.cycle_share), 1):
        loop.rank = i
    report.loops.sort(key=lambda l: l.rank)
    report.coverage = min(1.0, sum(l.cycle_share for l in report.loops))
    report.profile_method = profile.get("profile_method", report.profile_method)
    report.profile_runs = [total]
    report.profile_command = profile.get("command", report.profile_command)
    return report


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run_red_loop(project: str, core: str, workload: str, cflags: str,
                 run_id: str, work_root: str, archive_dir: str | None = None,
                 llm=None, tools=None,
                 harness: str = "", exclude: str = "") -> REDResult:
    """One end-to-end RED pass: A1 + Gate 1 -> HotLoopReport, then A2 + link 1
    (with a bounded repair edge) -> ISASpec."""
    tag = run_id.rsplit("-", 1)[-1]
    os.makedirs(work_root, exist_ok=True)
    profile_dir = os.path.join(work_root, "profile")
    llm_logs_dir = os.path.join(work_root, "llm_logs")
    for d in (profile_dir, llm_logs_dir):
        os.makedirs(d, exist_ok=True)

    core_path = os.path.join(work_root, "CoreProfile.yaml")
    profile_path = os.path.join(work_root, "profile.yaml")
    report_path = os.path.join(work_root, "HotLoopReport.yaml")
    spec_path = os.path.join(work_root, "ISASpec.yaml")
    status_path = os.path.join(work_root, "status.md")
    knowledge_path = os.path.join(work_root, "knowledge.md")
    sentinel_path = os.path.join(work_root, "finished")
    transcript_path = os.path.join(llm_logs_dir, "transcript.log")

    if tools is None:
        # One tool server, not seven. Each server is a uvicorn app on its own
        # loopback port and every agent turn re-reads the MCP config, so a
        # server per artifact cost startup time and config size for nothing
        # (see red/tools.py). The split that does exist is by what an agent is
        # allowed to see, not by artifact.
        tools = [DesignerTool(
            name=f"red_dsn_{tag}",
            source_roots=[project, *core_source_roots(core)],
            profile_path=profile_path, report_path=report_path,
            spec_path=spec_path, status_path=status_path,
            knowledge_path=knowledge_path, sentinel_path=sentinel_path,
            core_path=core_path, workload=workload, run_id=run_id)]
    finish_tool = next((t for t in tools if hasattr(t, "was_finished")), None)
    if finish_tool is not None:
        finish_tool.reset()
    tm = _tool_map(tools)

    def _mirror(rel: str, content) -> None:
        """Archive one artifact to the database node AND beside the run.

        The local copy is not redundancy for its own sake: the run directory
        under output/work is what the operator actually reads while a run is in
        flight, and it is routinely deleted between batches, whereas the store
        under RED_DB_ROOT is the record that survives. Writing both as the
        artifact is produced means a killed run still explains itself.
        """
        try:
            here = os.path.join(work_root, rel)
            os.makedirs(os.path.dirname(here), exist_ok=True)
            with open(here, "w") as f:
                f.write(content if isinstance(content, str) else str(content))
        except OSError as exc:
            print(f"[red] could not write local copy of {rel}: {exc}")
        if archive_dir:
            db_node.put(archive_dir, rel, content, workload=workload)

    # The persistent knowledge graph (red/graph.py). Opened here so a run is
    # recorded from its first stage; every call is a no-op when Neo4j is not
    # running, so a design run never depends on it.
    run_started = time.monotonic()
    # Per-run telemetry: a batch runs several projects in one process,
    # so the turn log has to start empty for each.
    rsi.reset_turns()
    graph.start_run(workload, run_id, project, core,
                    model=_model_tag(llm) or "claude-code",
                    harness=harness, cflags=cflags)

    gate0, gate1, gate2 = None, None, None
    iss_tools: list = []          # built lazily when Gate 2's implementer runs
    sec_tools: list = []          # built lazily when Gate 4's reviewer runs
    wl_tools: list = []           # built lazily when AW writes the workload
    workload_spec = None
    synthesized = ""              # AW's harness source, when it wrote one
    chain: list[state_def.GateResult] = []
    report, spec = None, None
    iters = 0
    try:
        # ---------------- AW: write the workload, Gate 0 measures it --------
        # RED's input is "a project + representative workloads" and the second
        # half is usually missing: what a repository ships is a unit test.
        # libcrc's own test suite executes 272,664 instructions and micro-ecc's
        # ECDH test executes 10,302,000,000 — the same loop cannot profile both,
        # and mining the former yields hot "loops" in the dynamic loader.
        #
        # So when the operator does not name a harness, an agent reads the
        # repository and writes one, and Gate 0 decides whether it is a
        # workload by running it: enough instructions, enough of them inside the
        # project's own code (from the binary's symbol table, not from the
        # agent's description), and the same answer twice.
        if not harness and llm is not None:
            wl_dir = os.path.join(work_root, "workload")
            os.makedirs(wl_dir, exist_ok=True)
            wl_path = os.path.join(
                wl_dir, f"{os.path.basename(project.rstrip('/'))}_workload.c")
            wl_status = os.path.join(wl_dir, "gate0.md")
            wl_tools.append(WorkloadTool(
                name=f"red_wl_{tag}", source_roots=[project],
                harness_path=wl_path, status_path=wl_status,
                knowledge_path=knowledge_path))
            wtm = _tool_map(wl_tools)
            for round_ in range(1, WORKLOAD_MAX_ROUNDS + 1):
                designer_charter, llm.system_message = llm.system_message, ""
                try:
                    _dispatch_llm(llm, _load_prompt(
                        "workload.md",
                        PROJECT=project,
                        LIST_SOURCES=wtm.get("list_sources", ""),
                        READ_SOURCE=wtm.get("read_source", ""),
                        WRITE_HARNESS=wtm.get("write_harness", ""),
                        READ_STATUS=wtm.get("read_status", ""),
                        READ_KNOWLEDGE=wtm.get("read_knowledge", ""),
                        MIN_IR=f"{WORKLOAD_MIN_IR:,}",
                        MIN_SHARE=f"{WORKLOAD_MIN_PROJECT_SHARE:.0%}",
                        DETERMINISM=f"{WORKLOAD_DETERMINISM:.0%}",
                        MAX_ROUNDS=str(WORKLOAD_MAX_ROUNDS),
                        STATUS=(workload_mod.render(workload_spec)
                                if workload_spec else
                                "Nothing has been measured yet — this is the "
                                "first round.")),
                        wl_tools, tag=f"AW workload {round_}", role="workload",
                        log_path=transcript_path)
                finally:
                    llm.system_message = designer_charter
                if not os.path.exists(wl_path):
                    print(f"[{run_id}] AW round {round_}: no harness written")
                    continue
                with open(wl_path) as f:
                    harness_source = f.read()
                raw = nodes.workload_check(
                    project, harness_source,
                    os.path.join(wl_dir, f"r{round_}"), cflags, exclude)
                workload_spec = state_def.from_yaml(raw, state_def.WorkloadSpec)
                gate0 = nodes.gate0(raw)
                with open(wl_status, "w") as f:
                    f.write(workload_mod.render(workload_spec))
                _mirror(f"workload/round{round_}.yaml",
                        state_def.to_yaml(workload_spec))
                _mirror(f"workload/round{round_}.md",
                        workload_mod.render(workload_spec))
                print(f"[{run_id}] Gate 0 round {round_}: "
                      f"{'PASS' if gate0.passed else 'FAIL'} — {gate0.detail}")
                if gate0.passed:
                    # A1 compiles it on the profile node, which has never seen
                    # this file: hand on the text, not the path.
                    synthesized = harness_source
                    workload_spec.entry = wl_path
                    break
            if gate0 is None:
                # The agent never produced a harness at all (its turns failed).
                # Record that as a failed Gate 0 rather than leaving it unset:
                # an unset gate0 reads downstream as "the operator supplied a
                # harness", which is the opposite of what happened.
                gate0 = state_def.GateResult(
                    gate="gate0", passed=False,
                    detail=f"AW produced no harness in {WORKLOAD_MAX_ROUNDS} "
                           "rounds (the agent's turns did not complete)")
            # AW is finished and nothing reads its tool server again. Every
            # server is a uvicorn app in this process, so one left running is
            # a thread and a port held for the rest of the run.
            for t in wl_tools:
                t.stop()
            wl_tools.clear()
            _mirror("gates/gate0.yaml", state_def.to_yaml(gate0))
            if not gate0.passed:
                # Profiling an unrepresentative workload does not produce a
                # worse extension, it produces a meaningless one: the mined
                # "hot loops" are the dynamic loader. Stop here and say so.
                print(f"[{run_id}] AW could not produce a representative "
                      f"workload for {project} — stopping before A1")
                graph.record_gate(workload, run_id, gate0)
                return REDResult(workload, False, iters, gate0=gate0,
                                 workload_spec=workload_spec)

        # ---------------- A1: profile x2 -> Gate 1 -> mining agent ----------
        prof_args = (project, workload, profile_dir, cflags, harness, exclude,
                     synthesized)
        # Gate 1 needs two independent profiles. They run in parallel threads
        # into sibling directories, so they cannot share a harness and the
        # second costs almost nothing in wall clock: callgrind is the slow part
        # and it is one process per profile.
        with cf.ThreadPoolExecutor(max_workers=2) as pool:
            prof1, prof2 = list(pool.map(
                lambda i: nodes.A1_profile_workload(
                    project, workload, os.path.join(profile_dir, f"run{i}"),
                    cflags, harness, exclude, synthesized),
                (1, 2)))
        _write_artifact(profile_path, prof1)
        _mirror("profile.yaml", state_def.to_yaml(prof1))

        total_a = sum(v for k, v in prof1.get("cycle_counts", {}).items() if k != "__total__")
        total_b = sum(v for k, v in prof2.get("cycle_counts", {}).items() if k != "__total__")
        gate1 = nodes.gate1(total_a, total_b)
        _mirror("gates/gate1.yaml", state_def.to_yaml(gate1))

        # ---------------- A0 core analysis, alongside A1 mining -------------
        # A2 used to design for an abstract RISC-V: it specified memory-operand
        # instructions for a coprocessor interface that cannot address memory,
        # and duplicated a multiplier the core already had. A0 reads the core's
        # own RTL and writes that down. It depends on the core, not the profile,
        # so it overlaps with mining rather than delaying it.
        mine_msg = _load_prompt(
            "mine.md",
            LIST_SOURCES=tm.get("list_sources", ""), READ_SOURCE=tm.get("read_source", ""),
            READ_PROFILE=tm.get("read_profile", ""), WRITE_REPORT=tm.get("write_report", ""))
        core_msg = _load_prompt(
            "core.md",
            LIST_SOURCES=tm.get("list_sources", ""), READ_SOURCE=tm.get("read_source", ""))
        mine_ref = _dispatch_async(llm, mine_msg, "A1 mining", tools,
                                   role="mine")
        core_ref = _dispatch_async(llm, core_msg, "A0 core analysis", tools,
                                   role="core")
        _resolve(mine_ref, "A1 mining", transcript_path)
        core_res = _resolve(core_ref, "A0 core analysis", transcript_path)

        core_profile = _parse_core_profile(core, core_res)
        graph.record_core_profile(core, core_profile)
        if core_profile is not None:
            _write_artifact(core_path, core_profile)
            _mirror("CoreProfile.yaml", state_def.to_yaml(core_profile))
            print(f"[{run_id}] A0: {core_profile.name} {core_profile.isa}, "
                  f"{core_profile.coprocessor} "
                  f"(memory access: {core_profile.coprocessor_memory_access}), "
                  f"{len(core_profile.constraints)} constraint(s)")
        report = _read_artifact(report_path, state_def.HotLoopReport) \
            or build_mechanical_report(project, workload, prof1)
        report = _merge_mechanical(report, prof1,
                                   source_roots=[project, core])
        report.passes_gate1 = gate1.passed
        _write_artifact(report_path, report)
        _mirror("HotLoopReport.yaml", state_def.to_yaml(report))
        graph.record_gate(workload, run_id, gate1)
        graph.record_hot_loops(workload, run_id, report)
        # Freeze it: from here on the report is the measurement A2's design is
        # scored against, and DesignerTool.write_report rejects any edit to it.
        with open(report_path + ".frozen", "w") as f:
            f.write("frozen after gate 1\n")

        # ---------------- A2: synthesis (S2a->S2b->S2c) -> Gate 2 + links ----
        if llm is not None:
            system = _load_prompt(
                "system.md",
                READ_REPORT=tm.get("read_report", ""), READ_PROFILE=tm.get("read_profile", ""),
                READ_STATUS=tm.get("read_status", ""), READ_KNOWLEDGE=tm.get("read_knowledge", ""),
                READ_SPEC=tm.get("read_spec", ""),
                WRITE_SPEC=tm.get("write_spec", ""), FINISH=tm.get("finish", ""),
                READ_CORE=tm.get("read_core", ""),
                GRAPH_WINNING_SHAPE=tm.get("graph_winning_shape", ""),
                GRAPH_PRIOR_DESIGNS=tm.get("graph_prior_designs", ""),
                GRAPH_PRIOR_FINDINGS=tm.get("graph_prior_findings", ""),
                GRAPH_BEST_DESIGN=tm.get("graph_best_design", ""),
                GRAPH_LOOPS=tm.get("graph_loops", ""),
                GRAPH_QUERY=tm.get("graph_query", ""),
                CORE_PROFILE=review.render_core(core_profile),
                CRITIC_MAX_ROUNDS=str(CRITIC_MAX_ROUNDS))
            # NOTE: each round is a fresh `claude -p` turn with no memory of
            # the last one, so the charter re-supplies context via the read
            # tools rather than assuming it (see red/prompts/system.md).
            llm.system_message = system
            consecutive_failures = 0
            for r in range(CRITIC_MAX_ROUNDS + 1):
                iters += 1
                msg = ("Read the HotLoopReport and produce the ISASpec (S2a -> S2b -> S2c), "
                       "then call finish." if r == 0 else
                       f"Critique round {r}/{CRITIC_MAX_ROUNDS}: your previous turn did not "
                       "finish. Attack the current draft (ambiguity, non-totality, encoding "
                       "collisions), revise it, and call finish when clean.")
                res = _dispatch_llm(llm, msg, tools, tag=f"A2 round {r}",
                                    role="design", log_path=transcript_path)
                consecutive_failures = (consecutive_failures + 1
                                        if res is not None and not getattr(res, "success", True)
                                        else 0)
                if finish_tool.was_finished():
                    break
                if consecutive_failures >= 2:
                    print(f"[{run_id}] A2: LLM backend failing on every turn — "
                          "aborting synthesis instead of burning the remaining rounds")
                    break

        spec = _read_artifact(spec_path, state_def.ISASpec)
        if spec is None:
            print(f"[{run_id}] A2: no ISASpec produced "
                  f"{'(LLM backend not available)' if llm is None else ''} — stopping before Gate 2")
            return REDResult(workload, False, iters, gate0=gate0, gate1=gate1,
                             gate2=gate2, chain=chain, report=report, spec=spec,
                             workload_spec=workload_spec)

        spec.critic_rounds = min(iters, CRITIC_MAX_ROUNDS)
        # link-1 status is the loop's to record, never the agent's to claim.
        spec.passes_link1 = False
        spec.link1_vectors = 0
        _write_artifact(spec_path, spec)
        _mirror("ISASpec.draft.yaml", state_def.to_yaml(spec))
        report_yaml = state_def.to_yaml(report)
        spec_yaml = state_def.to_yaml(spec)

        # ---------------- link 1, with a bounded agentic repair edge --------
        link1_dir = os.path.join(work_root, "link1")

        def _run_link1(stext: str) -> state_def.GateResult:
            return nodes.link1(report_yaml, stext, link1_dir)

        l1 = _run_link1(spec_yaml)
        # A failing link 1 is a concrete counterexample (a compile error, a
        # sanitizer report, a non-pure model). Hand it back to the agent through
        # the sealed status tool and let it revise — capped, then escalate.
        for repair in range(1, REPAIR_MAX_ROUNDS + 1):
            if l1.passed or llm is None:
                break
            print(f"[{run_id}] link1 FAILED — repair round {repair}/{REPAIR_MAX_ROUNDS}")
            _publish_status(status_path, l1, f"link 1 repair round {repair}")
            _mirror(f"gates/link1.repair{repair}.yaml", state_def.to_yaml(l1))
            iters += 1
            _dispatch_llm(llm, _load_prompt(
                "debug.md",
                READ_REPORT=tm.get("read_report", ""), READ_SPEC=tm.get("read_spec", ""),
                READ_PROFILE=tm.get("read_profile", ""), READ_STATUS=tm.get("read_status", ""),
                READ_KNOWLEDGE=tm.get("read_knowledge", ""),
                WRITE_REPORT=tm.get("write_report", ""), WRITE_SPEC=tm.get("write_spec", ""),
                APPEND_KNOWLEDGE=tm.get("append_knowledge", ""),
                VERDICT=f"{l1.gate}: {l1.detail}",
                COUNTEREXAMPLE=l1.counterexample or "(none)"),
                tools, tag=f"link1 repair {repair}", role="repair",
                log_path=transcript_path)
            revised = _read_artifact(spec_path, state_def.ISASpec)
            if revised is None:
                break
            spec = revised
            spec_yaml = state_def.to_yaml(spec)
            l1 = _run_link1(spec_yaml)

        # ---------------- Gate 3: is it actually faster? --------------------
        # The gates above establish that an instruction means what it says.
        # This one asks what it costs. RED shipped a verified extension that was
        # 2% slower than the baseline at +74% area (eval/RESULTS.md) because
        # nothing ever asked; red/cost.py is that question, calibrated against
        # the RTL those numbers came from. A design that cannot beat the
        # software it replaces goes back to the designer with the arithmetic.
        perf = None
        ship_bar = 0.0   # the bar gate 3 finally judged at (for metrics.yaml)
        plat = cost.Platform.from_core(core_profile)
        # A redesign is not guaranteed to be an improvement — one run went
        # 1.64x, then 0.96x, then 8.33x. Keep the best spec seen so the loop
        # never hands on a design worse than one it already had.
        best_spec_yaml, best_perf = None, None
        # Which Gate 3 round the loop actually reached. The shipped re-price is
        # judged at the bar that was in force then, not at the most relaxed bar
        # the schedule would ever have offered — otherwise the final check is
        # always at the floor and the schedule means nothing where it matters.
        perf_rounds_used = 1
        if llm is not None:
            for round_ in range(1, PERF_MAX_ROUNDS + 1):
                # Measure what each C model really does before costing it:
                # `mac_ops` is the designer's own claim about the price of its
                # design, and a review round caught a spec declaring 0 for a
                # 512-iteration reduction.
                measured = cost.measure_ops(spec, os.path.join(work_root, "gate3"))
                perf = cost.estimate(spec, report, core_profile, measured)
                _mirror(f"perf/round{round_}.yaml", state_def.to_yaml(perf))
                graph.record_spec(workload, run_id, spec, stage=f"gate3.{round_}")
                graph.record_perf(workload, run_id, perf, round_)
                _mirror(f"perf/round{round_}.md",
                        cost.render_feedback(
                            perf, target=cost.target_for(
                                round_, perf.covered_share)[0], platform=plat))
                if perf.measurement_error:
                    # The profile, not the design, is unusable. Redesigning
                    # cannot help, and pretending otherwise burns the budget
                    # and reports a bad extension instead of a broken profiler.
                    print(f"[{run_id}] Gate 3 cannot score any design: "
                          f"{perf.measurement_error}")
                    break
                regressed = (best_perf is not None
                             and perf.app_speedup < best_perf.app_speedup)
                # The bar this round: the ambition, capped by what Amdahl allows
                # on the share this design covers, relaxed as redesigns fail to
                # reach it. A fixed 2.00x asked micro-ecc for more than its
                # 47.3% coverage can ever yield.
                perf_rounds_used = round_
                bar, why = cost.target_for(round_, perf.covered_share)
                print(f"[{run_id}] Gate 3 round {round_}: predicted "
                      f"{perf.app_speedup:.2f}x over {perf.covered_share:.0%} of "
                      f"cycles (bar {bar:.2f}x"
                      + (f" — {why}" if why else "") + ")"
                      + (f" — worse than round {round_ - 1}'s "
                         f"{best_perf.app_speedup:.2f}x" if regressed else ""))
                if best_perf is None or perf.app_speedup > best_perf.app_speedup:
                    best_spec_yaml, best_perf = spec_yaml, perf
                if perf.meets_target(bar):
                    break
                if round_ == PERF_MAX_ROUNDS:
                    # Hand on the best design, not the most recent one.
                    if best_spec_yaml is not None and best_perf is not perf:
                        print(f"[{run_id}] Gate 3 budget spent; keeping round "
                              f"{best_perf.app_speedup:.2f}x over the final "
                              f"{perf.app_speedup:.2f}x")
                        spec = state_def.from_yaml(best_spec_yaml, state_def.ISASpec)
                        spec_yaml = best_spec_yaml
                        perf = best_perf
                        _write_artifact(spec_path, spec)
                        l1 = _run_link1(spec_yaml)
                    print(f"[{run_id}] Gate 3 budget spent at "
                          f"{perf.app_speedup:.2f}x — escalating")
                    break
                with open(status_path, "w") as f:
                    f.write(cost.render_feedback(perf, target=bar,
                                                 platform=plat))
                iters += 1
                _dispatch_llm(llm, _load_prompt(
                    "redesign.md",
                    READ_SPEC=tm.get("read_spec", ""), READ_REPORT=tm.get("read_report", ""),
                    READ_STATUS=tm.get("read_status", ""), WRITE_SPEC=tm.get("write_spec", ""),
                    APPEND_KNOWLEDGE=tm.get("append_knowledge", ""),
                    FINISH=tm.get("finish", ""),
                    TARGET=f"{cost.target_for(round_ + 1, perf.covered_share)[0]:.2f}",
                    VERDICT=(cost.render_feedback(perf, target=bar, platform=plat)
                             + (f"\n**Your last change made this worse** — the "
                                f"previous design reached "
                                f"{best_perf.app_speedup:.2f}x. Reconsider it "
                                f"rather than continuing in this direction.\n"
                                if regressed else ""))),
                    tools, tag=f"Gate 3 redesign {round_}", role="redesign",
                    log_path=transcript_path)
                revised = _read_artifact(spec_path, state_def.ISASpec)
                if revised is None:
                    break
                spec = revised
                spec_yaml = state_def.to_yaml(spec)
                l1 = _run_link1(spec_yaml)
                _mirror(f"gates/link1.perf{round_}.yaml", state_def.to_yaml(l1))
            final_bar, final_why = (
                cost.target_for(min(round_, PERF_MAX_ROUNDS), perf.covered_share)
                if perf else (SPEEDUP_TARGET, ""))
            gate3 = state_def.GateResult(
                gate="gate3", passed=bool(perf and perf.meets_target(final_bar)),
                detail=(f"predicted {perf.app_speedup:.2f}x over "
                        f"{perf.covered_share:.0%} of cycles "
                        f"(bar {final_bar:.2f}x"
                        + (f"; {final_why}" if final_why else "") + ")"
                        if perf else "not run"),
                counterexample=None if (perf and perf.meets_target(final_bar))
                else (perf.detail if perf else None))
            _mirror("gates/gate3.yaml", state_def.to_yaml(gate3))

        # ---------------- Gate 4: is it safe to build into the machine? -----
        # The gates above ask whether the extension means what it says and
        # whether it is fast enough to be worth building. Neither asks what it
        # does to the machine's security, and an ISA extension is the worst
        # possible place to find that out later: it is in the silicon, at the
        # privilege of whoever issues it, and it cannot be patched. On the
        # kernels RED mines the operands ARE the secret, so a conditional
        # subtraction in a modular reduction leaks the private key through
        # timing no matter how correct and how fast the instruction is.
        #
        # red/security.py answers that question the way the rest of this loop
        # answers questions -- by running something. Sanitizers, instruction
        # counts and decoded encoding bits decide the gate. The sub-agent
        # dispatched here is genuinely useful and genuinely unreliable, so it
        # gets the one power that makes its judgement checkable: it proposes
        # operand values it believes are dangerous, the loop *executes* them,
        # and only what actually breaks becomes a blocking finding.
        sec_report, gate4 = None, None
        sec_dir = os.path.join(work_root, "security")
        security_md = os.path.join(sec_dir, "report.md")
        agent_findings_path = os.path.join(sec_dir, "agent_findings.yaml")
        core_yaml = state_def.to_yaml(core_profile) if core_profile else ""
        os.makedirs(sec_dir, exist_ok=True)
        security_revisions = 0

        def _scan(stext: str, where: str) -> state_def.SecurityReport:
            raw = nodes.security_scan(stext, core_yaml, where)
            return state_def.from_yaml(raw, state_def.SecurityReport)

        if llm is not None:
            for round_ in range(1, SECURITY_MAX_ROUNDS + 1):
                sec_report = _scan(spec_yaml,
                                   os.path.join(sec_dir, f"round{round_}"))
                with open(security_md, "w") as f:
                    f.write(security.render(sec_report))
                if os.path.exists(agent_findings_path):
                    os.remove(agent_findings_path)
                if not sec_tools:
                    sec_tools.append(SecurityTool(
                        name=f"red_sec_{tag}", spec_path=spec_path,
                        core_path=core_path, security_path=security_md,
                        findings_path=agent_findings_path, probe_dir=sec_dir,
                        knowledge_path=knowledge_path))
                stm = _tool_map(sec_tools)
                # Clear the designer's charter for the turn, exactly as the
                # review does: an agent told it is the designer audits its own
                # work and finds nothing.
                designer_charter, llm.system_message = llm.system_message, ""
                try:
                    _dispatch_llm(llm, _load_prompt(
                        "security.md",
                        CORE_PROFILE=review.render_core(core_profile),
                        MECHANICAL=security.render(sec_report),
                        DIGEST=_spec_digest(spec),
                        READ_SPEC=stm.get("read_spec", ""),
                        READ_CORE=stm.get("read_core", ""),
                        READ_SECURITY_REPORT=stm.get("read_security_report", ""),
                        READ_KNOWLEDGE=stm.get("read_knowledge", ""),
                        PROBE_VECTOR=stm.get("probe_vector", ""),
                        REPORT_FINDINGS=stm.get("report_findings", ""),
                        MAX_PROBES=str(SECURITY_MAX_PROBES)),
                        sec_tools, tag=f"security agent {round_}", role="security",
                        log_path=transcript_path)
                finally:
                    llm.system_message = designer_charter
                sec_report = security.merge(sec_report,
                                            _read_findings(agent_findings_path))
                gate4 = security.verdict(sec_report)
                _mirror(f"security/round{round_}.yaml",
                        state_def.to_yaml(sec_report))
                _mirror(f"security/round{round_}.md", security.render(sec_report))
                graph.record_security(workload, run_id, sec_report, round_)
                n_hard = len(sec_report.mechanical_blocking())
                n_agent = sum(1 for f in sec_report.findings
                              if f.confidence == "agent")
                print(f"[{run_id}] Gate 4 round {round_}: {gate4.detail} "
                      f"({n_agent} from the security agent)")
                for f in sec_report.mechanical_blocking():
                    print(f"[{run_id}]   [{f.check}] "
                          f"{f.instruction or 'spec-wide'}: {f.finding}")
                if gate4.passed:
                    break
                if round_ == SECURITY_MAX_ROUNDS:
                    print(f"[{run_id}] security budget spent with {n_hard} "
                          "confirmed blocking finding(s) — escalating")
                    break
                with open(status_path, "w") as f:
                    f.write(security.render(sec_report))
                iters += 1
                security_revisions += 1
                _dispatch_llm(llm, _load_prompt(
                    "secure.md",
                    READ_SPEC=tm.get("read_spec", ""),
                    READ_REPORT=tm.get("read_report", ""),
                    READ_STATUS=tm.get("read_status", ""),
                    READ_CORE=tm.get("read_core", ""),
                    WRITE_SPEC=tm.get("write_spec", ""),
                    APPEND_KNOWLEDGE=tm.get("append_knowledge", ""),
                    FINISH=tm.get("finish", ""),
                    FINDINGS=security.render(sec_report)),
                    tools, tag=f"security revision {round_}", role="security_repair",
                    log_path=transcript_path)
                revised = _read_artifact(spec_path, state_def.ISASpec)
                if revised is None:
                    break
                spec = revised
                spec_yaml = state_def.to_yaml(spec)
                # The models changed, so their meaning has to be re-established
                # before anything downstream trusts them again.
                l1 = _run_link1(spec_yaml)
                _mirror(f"gates/link1.security{round_}.yaml",
                        state_def.to_yaml(l1))

            # A branch-free rewrite is not free: it changes what the model
            # executes, and Gate 3 priced the model it was given. Re-price the
            # spec that actually survived, rather than reporting a speedup for
            # a design that no longer exists.
            if security_revisions:
                measured = cost.measure_ops(
                    spec, os.path.join(work_root, "gate3", "post_security"))
                perf = cost.estimate(spec, report, core_profile, measured)
                gate3 = state_def.GateResult(
                    gate="gate3", passed=bool(perf.meets_target()),
                    detail=(f"predicted {perf.app_speedup:.2f}x over "
                            f"{perf.covered_share:.0%} of cycles (target "
                            f"{SPEEDUP_TARGET:.2f}x, re-priced after "
                            f"{security_revisions} security revision(s))"),
                    counterexample=None if perf.meets_target() else perf.detail)
                _mirror("gates/gate3.yaml", state_def.to_yaml(gate3))
                graph.record_perf(workload, run_id, perf, PERF_MAX_ROUNDS + 1)
                print(f"[{run_id}] Gate 3 re-priced after security: "
                      f"{perf.app_speedup:.2f}x")

        # ---------------- spec review: sub-agents, then back to the designer
        # link 1 and Gate 2 establish that an instruction means what it says.
        # Whether it is worth building is a different question, and the
        # evaluation in eval/ showed a fully verified spec can still be slower
        # than the code it replaces. Four reviewers ask the questions the gates
        # do not, concurrently, and the designer answers them.
        review_report = None
        # Keep the best spec the review ever reached, exactly as Gate 3 keeps
        # its best design. A revision is not guaranteed to be an improvement:
        # two runs walked their blocking findings down to 1 and then
        # regressed on the final round (1 -> 4 and 1 -> 3, one of them also
        # breaking a C model so link 1 fell to 1/2), and the loop reported that
        # regression because it handed on the *latest* spec instead of the best
        # one. Ranked by blocking findings first, then link 1, then total
        # findings -- never by anything the designer asserts about itself.
        best_review = None       # (rank, spec_yaml, review_report, l1)
        if llm is not None:
            for round_ in range(1, REVIEW_MAX_ROUNDS + 1):
                # Clear the A2 charter first: a reviewer told it is the designer
                # reviews its own work. Each reviewer's whole brief is its
                # prompt, and llm is pickled per dispatch, so this is enough to
                # keep the roles apart.
                designer_charter, llm.system_message = llm.system_message, ""
                try:
                    review_report = review.review_spec(
                        llm, spec_yaml, report_yaml, core,
                        dispatch=lambda msg, tag_: _dispatch_async(
                            llm, msg, tag_, role="review"),
                        core_profile=core_profile, prior=review_report,
                        perf=perf, security=sec_report)
                finally:
                    llm.system_message = designer_charter
                # The structural check runs on the same spec the reviewers
                # saw and merges into their report, so the revise round acts
                # on it the same way. It is mechanical: it measures operand
                # blocks and declared state rather than judging taste, which
                # is why its blocking findings are not negotiable the way a
                # reviewer's opinion is.
                struct = structure.check(spec, core_profile)
                if struct:
                    review_report.findings.extend(struct)
                    if structure.REVIEWER not in review_report.reviewers:
                        review_report.reviewers.append(structure.REVIEWER)
                    order = {"blocking": 0, "major": 1, "minor": 2}
                    review_report.findings.sort(
                        key=lambda f: order.get(f.severity, 3))
                print(f"[{run_id}] structure: {structure.summary(spec)}"
                      + (f" — {sum(1 for f in struct if f.severity == 'blocking')} "
                         f"blocking, {len(struct)} finding(s)" if struct else " — clean"))
                _mirror(f"review/round{round_}.yaml",
                        state_def.to_yaml(review_report))
                graph.record_spec(workload, run_id, spec, stage=f"review.{round_}")
                graph.record_review(workload, run_id, review_report, round_)
                _mirror(f"review/round{round_}.md", review.render(review_report))
                n_block = review_report.blocking()
                # Re-price this round's spec. Cheap (a compile and one callgrind
                # pass per model, no agent) and it is the only thing that can
                # tell a revision that answered the reviewers from one that
                # answered them by giving up the design.
                round_perf, round_bar = perf, None
                try:
                    round_perf = cost.estimate(
                        spec, report, core_profile,
                        cost.measure_ops(spec, os.path.join(
                            work_root, "gate3", f"review{round_}")))
                    round_bar = cost.target_for(perf_rounds_used,
                                                round_perf.covered_share)[0]
                except Exception as exc:              # noqa: BLE001
                    print(f"[{run_id}] could not re-price review round "
                          f"{round_}: {exc}")
                rank = _review_rank(review_report, l1, round_perf, round_bar)
                regressed = best_review is not None and rank > best_review[0]
                print(f"[{run_id}] review round {round_}: "
                      f"{len(review_report.findings)} finding(s), {n_block} blocking "
                      f"({', '.join(review_report.reviewers) or 'no reviewer replied'})"
                      + (f" — worse than the best so far "
                         f"({best_review[2].blocking()} blocking)" if regressed else ""))
                if best_review is None or rank < best_review[0]:
                    best_review = (rank, spec_yaml, review_report, l1,
                                   round_perf)
                # Clearing the findings is not the goal; shipping something
                # worth building is. A revision that answers every reviewer by
                # making the extension pointless used to end the loop here,
                # because "0 blocking" was the whole exit condition: matrixmul
                # took a design Gate 3 priced at 16.84x — the shape whose RTL
                # measures 14.57x — down to 1.07x, cleared its last finding
                # doing it, and the loop called that done. Keep going while
                # there is budget, and tell the designer what it gave away.
                gave_away = (round_perf is not None and round_bar is not None
                             and not round_perf.meets_target(round_bar))
                if not n_block and not gave_away:
                    break
                if not n_block and gave_away:
                    print(f"[{run_id}] review round {round_}: 0 blocking, but "
                          f"the revision priced at {round_perf.app_speedup:.2f}x "
                          f"against a {round_bar:.2f}x bar — continuing rather "
                          "than shipping a design the reviewers approved into "
                          "uselessness")
                if round_ == REVIEW_MAX_ROUNDS:
                    # Hand on the best spec the review reached, not the last one.
                    if best_review is not None and best_review[2] is not review_report:
                        print(f"[{run_id}] review budget spent; keeping the round "
                              f"with {best_review[2].blocking()} blocking finding(s) "
                              f"over the final {n_block}")
                        spec_yaml = best_review[1]
                        spec = state_def.from_yaml(spec_yaml, state_def.ISASpec)
                        review_report = best_review[2]
                        _write_artifact(spec_path, spec)
                        l1 = _run_link1(spec_yaml)
                        _mirror("gates/link1.review_best.yaml", state_def.to_yaml(l1))
                        n_block = review_report.blocking()
                    print(f"[{run_id}] review budget spent with {n_block} "
                          "blocking finding(s) outstanding — escalating to the "
                          "summary")
                    break
                # Hand the findings to the designer that wrote the spec.
                with open(status_path, "w") as f:
                    f.write(review.render(review_report))
                iters += 1
                _dispatch_llm(llm, _load_prompt(
                    "revise.md",
                    READ_SPEC=tm.get("read_spec", ""), READ_REPORT=tm.get("read_report", ""),
                    READ_STATUS=tm.get("read_status", ""), WRITE_SPEC=tm.get("write_spec", ""),
                    READ_CORE=tm.get("read_core", ""),
                    APPEND_KNOWLEDGE=tm.get("append_knowledge", ""),
                    FINISH=tm.get("finish", ""),
                    FINDINGS=(review.render(review_report)
                              + ((f"\n\n## Your last revision gave away the "
                                  f"performance\n\nThe draft before it was "
                                  f"priced at {best_review[4].app_speedup:.2f}x; "
                                  f"this one prices at "
                                  f"{round_perf.app_speedup:.2f}x. Answering a "
                                  f"reviewer is not worth having if the "
                                  f"extension stops being worth building — the "
                                  f"findings have to be answered *and* the "
                                  f"speedup kept. Go back to the faster design "
                                  f"and make the smallest change that addresses "
                                  f"the findings.\n")
                                 if (round_perf is not None and best_review
                                     and best_review[4] is not None
                                     and round_perf.app_speedup
                                     < best_review[4].app_speedup * 0.5) else "")
                              + (f"\n\n## Your last revision made this worse\n\n"
                                 f"The previous draft had "
                                 f"{best_review[2].blocking()} blocking "
                                 f"finding(s); this one has {n_block}. The loop "
                                 f"keeps the better draft, so you are revising "
                                 f"from it — but changing more is not working. "
                                 f"Make the smallest change that answers the "
                                 f"findings above and change nothing else.\n"
                                 if regressed and best_review else ""))),
                    tools, tag=f"designer revision {round_}", role="revise",
                    log_path=transcript_path)
                revised = _read_artifact(spec_path, state_def.ISASpec)
                if revised is None:
                    break
                spec = revised
                spec_yaml = state_def.to_yaml(spec)
                # The spec changed under it, so link 1 must be re-established
                # before the revised models can be trusted.
                l1 = _run_link1(spec_yaml)
                _mirror(f"gates/link1.review{round_}.yaml", state_def.to_yaml(l1))

        # ---------------- Gate 2: the ISS implementer, then the diff --------
        gate2_dir = os.path.join(work_root, "gate2")
        # The implementer gets its own sealed tools: a spec view with the C
        # models hidden (IssSpecTool), the last verdict, and the shared notes.
        if llm is not None and not iss_tools:
            iss_tools.append(IssTool(
                name=f"red_iss_{tag}", spec_path=spec_path,
                status_path=status_path, knowledge_path=knowledge_path))

        def _run_gate2(stext: str) -> state_def.GateResult:
            return nodes.gate2(stext, gate2_dir)

        gate2 = None
        for attempt in range(REPAIR_MAX_ROUNDS + 1):
            # A separate agent implements every instruction for Spike, working
            # from the spec's prose and encoding only — its sealed tool hides the
            # C models. Two independent readings of the same spec is the whole
            # basis of the comparison that follows.
            if llm is not None and _dispatch_iss(
                    llm, iss_tools, transcript_path,
                    f"ISS implementer (attempt {attempt + 1})") is None:
                break
            spec = _read_artifact(spec_path, state_def.ISASpec) or spec
            spec_yaml = state_def.to_yaml(spec)
            gate2 = _run_gate2(spec_yaml)
            if gate2.passed or llm is None or attempt == REPAIR_MAX_ROUNDS:
                break
            # A divergence means the two implementers read the same words
            # differently: the spec was ambiguous. Send it back to be sharpened.
            print(f"[{run_id}] gate2 FAILED — repair round "
                  f"{attempt + 1}/{REPAIR_MAX_ROUNDS}: {gate2.detail}")
            _mirror(f"gates/gate2.repair{attempt + 1}.yaml", state_def.to_yaml(gate2))
            _publish_status(status_path, gate2)
            iters += 1
            _dispatch_llm(llm, _load_prompt(
                "debug.md",
                READ_REPORT=tm.get("read_report", ""), READ_SPEC=tm.get("read_spec", ""),
                READ_PROFILE=tm.get("read_profile", ""), READ_STATUS=tm.get("read_status", ""),
                READ_KNOWLEDGE=tm.get("read_knowledge", ""),
                WRITE_REPORT=tm.get("write_report", ""), WRITE_SPEC=tm.get("write_spec", ""),
                APPEND_KNOWLEDGE=tm.get("append_knowledge", ""),
                VERDICT=f"{gate2.gate}: {gate2.detail}",
                COUNTEREXAMPLE=gate2.counterexample or "(none)"),
                tools, tag=f"gate2 repair {attempt + 1}", role="repair")
            spec = _read_artifact(spec_path, state_def.ISASpec) or spec
            # The spec changed, so the previous Spike models are stale — the
            # next round re-implements them from the revised wording.
            for ins in spec.instructions:
                ins.spike_model = ""
            _write_artifact(spec_path, spec)

        if gate2 is None:
            gate2 = _run_gate2(state_def.to_yaml(spec))
        l2 = nodes.link2(state_def.to_yaml(gate2))
        _mirror("gates/gate2.yaml", state_def.to_yaml(gate2))

        chain = [l1, l2]
        _mirror("gates/chain.yaml", state_def.to_yaml(chain))

        # ---------------- finalize the ISASpec (RED's deliverable) ----------
        spec = _read_artifact(spec_path, state_def.ISASpec) or spec
        spec.passes_link1 = l1.passed
        spec.link1_vectors = LINK1_VECTORS if l1.passed else 0
        spec.passes_gate2 = gate2.passed
        spec.gate2_vectors = GATE2_VECTORS if gate2.passed else 0
        spec.critic_rounds = min(iters, CRITIC_MAX_ROUNDS)
        spec.tests = [f"link1: {l1.detail}", f"gate2: {gate2.detail}"] + (
            [f"counterexample: {l1.counterexample}"] if l1.counterexample else []) + (
            [f"counterexample: {gate2.counterexample}"] if gate2.counterexample else [])
        _write_artifact(spec_path, spec)
        _mirror("ISASpec.yaml", state_def.to_yaml(spec))

        # Gate 4, on the spec that actually ships. The review may have revised
        # the design after the security stage passed it, and a revision made for
        # one reviewer can reintroduce exactly what the security gate rejected —
        # a conditional subtraction put back to save a few cycles, an output
        # word dropped to narrow the block. So the final artifact is re-checked
        # mechanically. No agent runs here: it is the batteries alone, it costs
        # a compile and a few hundred vectors per model, and what it is guarding
        # is the only version anyone will ever build.
        if llm is not None:
            final_sec = _scan(state_def.to_yaml(spec),
                              os.path.join(sec_dir, "final"))
            # Carry the agent's advisory findings across before the verdict is
            # taken: they cannot change pass or fail, but the verdict's one-line
            # detail counts them, and a line that says "0 advisory" beside a
            # report holding one is a small lie in the wrong direction.
            if sec_report is not None:
                final_sec = security.merge(final_sec, [
                    f for f in sec_report.findings if f.confidence == "agent"])
            gate4 = security.verdict(final_sec)
            if sec_report is not None and gate4.passed and \
                    not security.verdict(sec_report).passed:
                print(f"[{run_id}] Gate 4: the security findings are resolved in "
                      "the shipped spec")
            elif not gate4.passed:
                print(f"[{run_id}] Gate 4 FAILED on the final spec: "
                      f"{gate4.detail}")
                for f in final_sec.mechanical_blocking():
                    print(f"[{run_id}]   [{f.check}] "
                          f"{f.instruction or 'spec-wide'}: {f.finding}")
                # ...and then fix it. A defect found here is one the review
                # reintroduced into a design the security stage had already
                # cleared, so it is a regression with a known fix, detected,
                # against a spec that is otherwise finished. Failing the run at
                # this point threw all of that away: matrixmul passed Gate 4
                # round 1 cleanly, the review revised the spec, the re-check
                # caught a data-dependent branch, and the run was reported not
                # converged for a defect nobody was ever given the chance to
                # repair. One round, then the verdict stands.
                for fix_round in range(1, SECURITY_MAX_ROUNDS + 1):
                    with open(status_path, "w") as f:
                        f.write(security.render(final_sec))
                    iters += 1
                    _dispatch_llm(llm, _load_prompt(
                        "secure.md",
                        READ_SPEC=tm.get("read_spec", ""),
                        READ_REPORT=tm.get("read_report", ""),
                        READ_STATUS=tm.get("read_status", ""),
                        READ_CORE=tm.get("read_core", ""),
                        WRITE_SPEC=tm.get("write_spec", ""),
                        APPEND_KNOWLEDGE=tm.get("append_knowledge", ""),
                        FINISH=tm.get("finish", ""),
                        FINDINGS=security.render(final_sec)),
                        tools, tag=f"final security repair {fix_round}",
                        role="security_repair",
                        log_path=transcript_path)
                    revised = _read_artifact(spec_path, state_def.ISASpec)
                    if revised is None:
                        break
                    spec = revised
                    spec_yaml = state_def.to_yaml(spec)
                    # The models changed under every downstream verdict, so
                    # re-establish them rather than shipping a spec whose
                    # link 1 and Gate 2 describe the previous version.
                    l1 = _run_link1(spec_yaml)
                    _mirror(f"gates/link1.finalsec{fix_round}.yaml",
                            state_def.to_yaml(l1))
                    final_sec = _scan(spec_yaml,
                                      os.path.join(sec_dir, f"final{fix_round}"))
                    gate4 = security.verdict(final_sec)
                    print(f"[{run_id}] final security repair {fix_round}: "
                          f"{'PASS' if gate4.passed else 'FAIL'} — {gate4.detail}")
                    if gate4.passed:
                        # Gate 2 verified the pre-repair wording; re-run it so
                        # the shipped spec's verdict is about the shipped spec.
                        for ins in spec.instructions:
                            ins.spike_model = ""
                        _write_artifact(spec_path, spec)
                        if _dispatch_iss(llm, iss_tools, transcript_path,
                                         "ISS implementer (after security repair)"):
                            spec = _read_artifact(spec_path,
                                                  state_def.ISASpec) or spec
                            spec_yaml = state_def.to_yaml(spec)
                            gate2 = _run_gate2(spec_yaml)
                            l2 = nodes.link2(state_def.to_yaml(gate2))
                            chain = [l1, l2]
                            _mirror("gates/gate2.yaml", state_def.to_yaml(gate2))
                            _mirror("gates/chain.yaml", state_def.to_yaml(chain))
                            print(f"[{run_id}] Gate 2 re-verified after the "
                                  f"security repair: "
                                  f"{'PASS' if gate2.passed else 'FAIL'}")
                        break
            sec_report = final_sec
            _mirror("security/final.yaml", state_def.to_yaml(sec_report))
            _mirror("security/final.md", security.render(sec_report))
            _mirror("gates/gate4.yaml", state_def.to_yaml(gate4))
            graph.record_security(workload, run_id, sec_report, 0)

        # Gate 3, on the spec that actually ships. Every other gate in the loop
        # is re-run when the artifact changes under it — link 1 after every
        # repair, redesign, security revision and review revision; Gate 4 on
        # whatever finally ships — and Gate 3 was the one place where the loop
        # reasoned about the artifact instead of re-measuring it. It runs before
        # Gate 4 and the review, and was re-priced only after a *security*
        # revision, so a reviewer could change the design afterwards and the
        # stale number stood.
        #
        # That is not hypothetical: all three shipped extensions were revised
        # after Gate 3 last ran. On micro-ecc the review merged two instructions
        # into one and gave up a mined loop; the run reported 11.60x, re-pricing
        # what shipped gives 1.85x, and the RTL subsequently measured 1.67x.
        # The entire error was bookkeeping.
        if llm is not None and spec is not None:
            measured = cost.measure_ops(
                spec, os.path.join(work_root, "gate3", "shipped"))
            shipped_perf = cost.estimate(spec, report, core_profile, measured)
            if perf is None or abs(shipped_perf.app_speedup
                                   - perf.app_speedup) > 0.01:
                print(f"[{run_id}] Gate 3 re-priced on the shipped spec: "
                      f"{shipped_perf.app_speedup:.2f}x"
                      + (f" (was {perf.app_speedup:.2f}x before the review "
                         f"revised it)" if perf else ""))
            perf = shipped_perf
            # The same bar the loop settled on, so the shipped verdict answers
            # the question the loop was actually asking by the end.
            ship_bar, ship_why = cost.target_for(perf_rounds_used,
                                                  perf.covered_share)
            gate3 = state_def.GateResult(
                gate="gate3", passed=bool(perf.meets_target(ship_bar)),
                detail=(f"predicted {perf.app_speedup:.2f}x over "
                        f"{perf.covered_share:.0%} of cycles (bar "
                        f"{ship_bar:.2f}x"
                        + (f"; {ship_why}" if ship_why else "")
                        + ", priced on the shipped spec)"),
                counterexample=None if perf.meets_target(ship_bar) else perf.detail)
            _mirror("gates/gate3.yaml", state_def.to_yaml(gate3))
            _mirror("perf/shipped.yaml", state_def.to_yaml(perf))
            _mirror("perf/shipped.md", cost.render_feedback(perf, platform=plat))
            graph.record_perf(workload, run_id, perf, PERF_MAX_ROUNDS + 2)

        # A spec with unresolved blocking findings has not converged, however
        # cleanly it verifies: the reviewers are asking whether it is worth
        # building, and the gates never do. Nor has one that is fast, correct
        # and leaks its operands — an extension that cannot be deployed safely
        # is not a design RED should report as finished.
        blocking = review_report.blocking() if review_report else 0
        fast_enough = bool(gate3 and gate3.passed)
        secure = bool(gate4 and gate4.passed)
        converged = (gate1.passed and l1.passed and gate2.passed
                     and fast_enough and secure and not blocking)

        # The deliverable and every verdict behind it, into the graph. The final
        # spec is recorded under its own stage so a later run can ask what the
        # design actually shipped as, not merely what it looked like mid-loop.
        graph.record_spec(workload, run_id, spec, stage="final")
        for g in [gate0, gate2, gate3, gate4] + list(chain):
            graph.record_gate(workload, run_id, g)
        graph.finish_run(
            workload, run_id, converged=converged,
            wallclock=time.monotonic() - run_started, blocking=blocking,
            speedup=(perf.app_speedup if perf else 0.0))

        # What this run cost and what it produced, for red.rsi. The round
        # counts are read back from the artifacts the loop mirrored rather
        # than threaded through every stage: the artifacts are written as
        # each round finishes, so a count taken from them is a count of
        # rounds that actually happened.
        def _rounds(sub: str, stem: str = "round") -> int:
            d = os.path.join(work_root, sub)
            if not os.path.isdir(d):
                return 0
            return sum(1 for n in os.listdir(d)
                       if n.startswith(stem) and n.endswith(".yaml"))

        gates_seen = [g for g in (gate0, gate1, gate2, gate3, gate4)
                      if g is not None]
        metrics = rsi.collect_turn_metrics(rsi.RunMetrics(
            workload=workload, run_id=run_id,
            started=datetime.datetime.now().astimezone().isoformat(
                timespec="seconds"),
            model_label=_model_tag(llm),
            frozen_fingerprint=rsi.frozen_fingerprint(_REPO_ROOT),
            converged=converged,
            gates_passed=sum(1 for g in gates_seen if g.passed),
            gates_total=len(gates_seen),
            coverage=float(report.coverage if report else 0.0),
            predicted_speedup=float(perf.app_speedup if perf else 0.0),
            speedup_bar=float(ship_bar or 0.0),
            blocking_findings=blocking,
            structure_blocking=sum(
                1 for f in (review_report.findings if review_report else [])
                if f.reviewer == structure.REVIEWER and f.severity == "blocking"),
            instructions=len(spec.instructions) if spec else 0,
            wallclock_seconds=round(time.monotonic() - run_started, 1),
            review_rounds=_rounds("review"),
            redesign_rounds=_rounds("perf"),
            repair_rounds=sum(1 for n in os.listdir(os.path.join(work_root, "gates"))
                              if n.startswith("link1.repair")) if os.path.isdir(
                                  os.path.join(work_root, "gates")) else 0,
            critic_rounds=int(spec.critic_rounds or 0) if spec else 0))
        _mirror("metrics.yaml", state_def.to_yaml(metrics))
        print(f"[{run_id}] metrics: {metrics.summary()}")

        return REDResult(workload, converged, iters, gate0=gate0, gate1=gate1,
                         gate2=gate2, gate3=gate3, gate4=gate4, chain=chain,
                         report=report, spec=spec, review=review_report,
                         security=sec_report, workload_spec=workload_spec,
                         model=_model_tag(llm))
    finally:
        # A run that returned early or died must not sit at status='running'.
        # finish_run has already fired on the normal path; this only catches the
        # rest.
        graph.abort_run(workload, run_id,
                        "the run ended without reaching a final verdict")
        # Ship the agent transcripts to the store too — the loop's audit trail is
        # the artifacts *plus* how the agents got there.
        try:
            if archive_dir and os.path.isdir(llm_logs_dir) and os.listdir(llm_logs_dir):
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    tar.add(llm_logs_dir, arcname=".")
                blob = buf.getvalue()
                db_node.archive_dir(archive_dir, "llm_logs", blob,
                                    workload=workload)
        except Exception as exc:  # noqa: BLE001 — archival must not fail the run
            print(f"[{run_id}] WARNING: could not archive LLM transcripts: {exc}")
        for t in list(tools) + iss_tools + sec_tools + wl_tools:
            t.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _render_summary(r: REDResult, run_n: int, run_name: str = "") -> str:
    def mark(g):
        return "PASS" if g and g.passed else "FAIL" if g else "—"
    chain_lines = "\n".join(f"- {mark(g)} {g.gate}: {g.detail}" for g in r.chain)
    coverage = f"{r.report.coverage:.2%} ({COVERAGE_TARGET:.0%} target)" if r.report else "n/a"
    instrs = "\n".join(
        f"  - `{i.mnemonic}` ({i.opcode}, {i.form}, {i.words}x32b, {i.group}): "
        f"{i.semantics}"
        for i in (r.spec.instructions if r.spec else []))
    state_lines = []
    for rf in (r.spec.register_files if r.spec else []):
        state_lines.append(f"  - register file `{rf.name}`: {rf.count} x "
                           f"{rf.width_bits} bits — {rf.purpose}")
    for csr in (r.spec.csrs if r.spec else []):
        state_lines.append(f"  - CSR `{csr.name}` @ {csr.address} "
                           f"({csr.access}) — {csr.purpose}")
    state_block = ("\n".join(state_lines) + "\n") if state_lines else ""
    shape_line = structure.summary(r.spec) if r.spec else "no spec"
    if r.review is None:
        review_line = "not run"
    elif not r.review.findings:
        review_line = f"clean ({', '.join(r.review.reviewers)})"
    else:
        review_line = (f"{r.review.blocking()} blocking, "
                       f"{len(r.review.findings)} total "
                       f"({', '.join(r.review.reviewers)})")
        for f_ in r.review.actionable()[:6]:
            review_line += (f"\n    - [{f_.severity}] "
                            f"{f_.instruction or 'spec'}: {f_.finding}")
    workload_line = ""
    if r.workload_spec is not None and r.workload_spec.entry:
        w = r.workload_spec
        workload_line = (f"    - {os.path.basename(w.entry)}: "
                         f"{w.dynamic_ir:,} instructions, "
                         f"{w.project_share:.1%} in the project's own code\n")
        if w.api:
            workload_line += ("    - drives: " + ", ".join(w.api[:6]) + "\n")
    security_line = ""
    if r.security is not None:
        advisory = [f_ for f_ in r.security.findings
                    if f_.severity in ("blocking", "major")
                    and not f_.is_mechanical()]
        for f_ in r.security.mechanical_blocking()[:6]:
            security_line += (f"    - [{f_.check}] "
                              f"{f_.instruction or 'spec'}: {f_.finding}\n")
        for f_ in advisory[:4]:
            security_line += (f"    - (advisory) {f_.instruction or 'spec'}: "
                              f"{f_.finding}\n")
        for skipped in r.security.checks_skipped[:4]:
            security_line += f"    - NOT CHECKED — {skipped}\n"
    return (f"# RED {run_name or f'run_{run_n}'} — {r.workload}\n\n"
            # The backend is whatever the routing resolved to, which since
            # per-stage routing arrived is frequently more than one of them.
            # Naming a fixed vendor here reported a Codex run as Claude Code.
            f"- backend: {r.model or 'unspecified'}\n"
            f"- result: **{'converged (A1+A2)' if r.converged else 'NOT converged'}**\n"
            f"- A2 rounds: {r.iterations}\n"
            f"- Gate 0 (workload): {mark(r.gate0)} "
            f"{r.gate0.detail if r.gate0 else '(operator-supplied harness)'}\n"
            f"{workload_line}"
            f"- Gate 1 (re-profile ±5%): {mark(r.gate1)} "
            f"{r.gate1.detail if r.gate1 else ''}\n"
            f"- Gate 2 (C model vs Spike): {mark(r.gate2)} "
            f"{r.gate2.detail if r.gate2 else ''}\n"
            f"- Gate 3 (predicted speedup): {mark(r.gate3)} "
            f"{r.gate3.detail if r.gate3 else ''}\n"
            f"- Gate 4 (security): {mark(r.gate4)} "
            f"{r.gate4.detail if r.gate4 else ''}\n{security_line}"
            f"- coverage: {coverage}\n"
            f"- spec review: {review_line}\n"
            f"- verification edges:\n{chain_lines}\n"
            f"- ISASpec: {r.spec.name if r.spec else 'none'} "
            f"({len(r.spec.instructions) if r.spec else 0} instructions)\n"
            f"- shape: {shape_line}\n"
            f"{state_block}{instrs}\n")


def _parse_args():
    p = argparse.ArgumentParser(description="RED loop — A1 kernel mining + A2 ISA synthesis.")
    p.add_argument("--project", default=EXAMPLE_PROJECT,
                   help="application project dir (sources + workloads)")
    p.add_argument("--core", default=EXAMPLE_CORE,
                   help="processor core dir — context for the designer (encoding space)")
    p.add_argument("--workload", default=EXAMPLE_WORKLOAD, help="workload name")
    # The harness + cflags are what actually define the workload; the defaults
    # pin the bundled example to ECDH over secp256r1 with 32-bit limbs.
    p.add_argument("--harness", default=EXAMPLE_HARNESS,
                   help="project-relative .c file holding the workload's main() "
                        "(default: the bundled ECDH test)")
    p.add_argument("--cflags", default=EXAMPLE_CFLAGS,
                   help="cc flags for the harness build (example default)")
    p.add_argument("--exclude", default="",
                   help="comma-separated path fragments whose .c files are NOT "
                        "part of this workload's program (e.g. 'precalc' for a "
                        "repo that also ships a code generator with its own "
                        "main). They stay readable by the agents; they are just "
                        "not linked.")
    p.add_argument("--run-id", default=None)
    p.add_argument("--run-prefix", default="run",
                   help="name the durable run directory <prefix>-<N> instead "
                        "of run_<N>, so a model sweep is legible on disk "
                        "(e.g. --run-prefix opus-5.5-run)")
    p.add_argument("--work-root", default=None)
    p.add_argument("--mechanical", action="store_true",
                   help="run the mechanical edges only — profile, Gate 1, the "
                        "typed artifacts — with no agents and no model spend")
    p.add_argument("--model", default=AGENT_MODEL,
                   help="model for the agent turns (default: whatever your "
                        "Claude Code install is configured to use)")
    p.add_argument("--effort", default=AGENT_EFFORT,
                   choices=["", "low", "medium", "high", "xhigh", "max"],
                   help="how hard the model thinks per turn, for stages the "
                        "routing file does not name")
    p.add_argument("--backend", default=modelcfg.DEFAULT_BACKEND,
                   choices=list(modelcfg.BACKENDS),
                   help="agent CLI for stages the routing file does not name")
    p.add_argument("--models-config", default=None,
                   help="per-stage model routing (default: models.toml beside "
                        "the repo, or RED_MODELS_CONFIG). Pass '' to disable "
                        "it and run every stage on --model/--effort.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    quiet_servers()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"{ts}-{uuid.uuid4().hex[:6]}"
    work_root = args.work_root or os.path.join(RED_LOG_ROOT, args.workload, run_id)

    # --mechanical runs the gates with no agents at all: the profile, Gate 1
    # and the typed artifacts, so the toolchain can be checked without spending
    # anything on a model.
    router = make_router("" if args.models_config is None else args.models_config,
                         model=args.model, effort=args.effort,
                         backend=args.backend)
    models = router.models
    llm = None if args.mechanical else router
    if not args.mechanical:
        # Print the resolved table, not the file. A routing surprise is the
        # kind of thing that is invisible in a result and obvious in a log.
        print(models.render())

    run_n, sweep_path = db_node.claim_run(args.workload, DB_ROOT,
                                          prefix=args.run_prefix)
    try:
        result = run_red_loop(args.project, args.core, args.workload, args.cflags,
                              run_id, work_root, archive_dir=sweep_path,
                              llm=llm, tools=([] if args.mechanical else None),
                              harness=args.harness, exclude=args.exclude)
    except Exception:
        import traceback
        traceback.print_exc()
        result = REDResult(args.workload, False, 0)

    summary = _render_summary(result, run_n,
                              os.path.basename(sweep_path))
    db_node.write_text(sweep_path, "summary.md", summary, workload=args.workload)
    print(f"\n{summary}\nartifacts -> {sweep_path}")
    _POOL.shutdown(wait=False)
    return 0 if result.converged else 1


if __name__ == "__main__":
    raise SystemExit(main())
