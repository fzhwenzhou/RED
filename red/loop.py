"""RED loop driver — orchestrate A1 kernel mining + A2 ISA synthesis.

A1 and A2 are the research contribution and RED's whole scope; the **ISASpec is
the final artifact**. Each stage is *agentic* (a Gemini agent over the sealed
MCP tools) and bounded by a *programmatic* edge from :mod:`red.nodes`:

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

Every artifact is persisted as typed JSON (HotLoopReport / ISASpec) and
mirrored to the durable DB sweep as it is produced.

Three run modes:
    default      dispatch nodes via .chia_remote() onto the CHIA cluster
    --local      call nodes in-process (no cluster resources) and skip the LLM
                 (llm=None) so the mechanical A1 + gates smoke-test on one
                 machine without credentials.
    --local-llm  in-process nodes, but with the real Gemini backend and the
                 sealed tools — the full A1+A2 loop on one machine.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import sys
import tarfile
import uuid
from dataclasses import dataclass, field

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import time

import ray
from ray.exceptions import GetTimeoutError

from chia.base.ChiaFunction import get
from chia.base.llm_call import QueryResult
from chia.models.vertex import VertexGeminiLLM
from chia.trace.profiler import start_collector, stop_collector

from red import cost, db_node, graph, nodes, review, security, state_def
from red.constants import (
    COVERAGE_TARGET,
    CRITIC_MAX_ROUNDS,
    DB_ROOT,
    EXAMPLE_CFLAGS,
    EXAMPLE_CORE,
    EXAMPLE_HARNESS,
    EXAMPLE_PROJECT,
    EXAMPLE_WORKLOAD,
    GCP_LOCATION,
    GCP_PROJECT,
    GATE2_VECTORS,
    LINK1_VECTORS,
    LLM_BACKEND,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_PROMPTS_DIR,
    LLM_RESOURCE,
    LLM_TIMEOUT_SECONDS,
    RED_LOG_ROOT,
    REPAIR_MAX_ROUNDS,
    PERF_MAX_ROUNDS,
    RES_DATABASE,
    RES_HEAD_LOCAL,
    RES_LLM,
    RES_PROFILE,
    RES_SPIKE,
    REVIEW_MAX_ROUNDS,
    SECURITY_MAX_PROBES,
    SECURITY_MAX_ROUNDS,
    SPEEDUP_TARGET,
    STAGE_MAX_SECONDS,
    STAGE_POLL_SECONDS,
    REPO_ROOT,
    find_sources,
)
from red.tools import DesignerTool, IssTool, RelayTool, SecurityTool

HEAD_LOCAL = {"resources": {"head_local": 0.1}}   # sealed-tool placement


# Resources a single-machine run advertises so every ChiaFunction (and the LLM
# backend) schedules locally — the cluster advertises the same tags per node.
LOCAL_RESOURCES = {"llm": 4, "head_local": 8, "database": 2, "profile": 4,
                   "spike": 2}


@dataclass
class REDResult:
    """End-to-end RED loop outcome the driver summarizes."""

    workload: str
    converged: bool
    iterations: int
    gate1: state_def.GateResult | None = None
    gate2: state_def.GateResult | None = None
    gate3: state_def.GateResult | None = None
    gate4: state_def.GateResult | None = None
    chain: list[state_def.GateResult] = field(default_factory=list)
    report: state_def.HotLoopReport | None = None
    spec: state_def.ISASpec | None = None
    review: state_def.ReviewReport | None = None
    security: state_def.SecurityReport | None = None


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

    The Vertex backend registers each MCP method as ``<tool>_<method>`` (the
    ``name=`` passed to ``mcp.add_tool`` in red/tools.py) and exposes it to the
    model as ``<tool>__<tool>_<method>`` (see vertex.py ``_run_generate_async``).

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


def _review_rank(report, link1_result) -> tuple:
    """How good a reviewed spec is, lowest is best.

    Blocking findings first — they are what convergence is defined by — then
    whether link 1 still passes, then total findings. Nothing here is anything
    the designer asserts about its own spec; every term is a gate's or a
    reviewer's verdict.
    """
    return (report.blocking(),
            0 if (link1_result is not None and link1_result.passed) else 1,
            len(report.findings))


def _await(ref, what: str, resource: str | None = None):
    """``get(ref)``, but bounded — and able to tell a slow stage from a dead one.

    A plain ``get()`` on a task whose resource has left the cluster waits
    forever. That is not theoretical: the three GCP workers' reverse SSH tunnels
    dropped mid-run, the ``llm`` resource disappeared from the scheduler, and the
    driver sat on one unschedulable turn for nine hours while the autoscaler
    logged "No available node types can fulfill resource request" twice a
    minute and the instances kept billing.

    A timeout alone would be the wrong fix, because some stages legitimately run
    for half an hour (callgrind over the whole ECDH exchange). So poll instead:
    every ``STAGE_POLL_SECONDS`` ask whether the resource this task needs still
    exists in the cluster. If it does, the work is merely slow and we keep
    waiting; if it has gone, fail immediately with something the operator can
    act on.
    """
    if ref is None:
        return None
    started = time.monotonic()
    while True:
        try:
            return get(ref, timeout=max(STAGE_POLL_SECONDS, 1.0))
        except GetTimeoutError:
            # Real elapsed time, not the sum of nominal polls: a poll that
            # returns early or overruns must still count toward the cap.
            waited = time.monotonic() - started
            if resource:
                try:
                    avail = ray.cluster_resources().get(resource, 0)
                except Exception:                             # noqa: BLE001
                    avail = 0
                if not avail:
                    raise RuntimeError(
                        f"{what}: the '{resource}' resource is no longer in the "
                        f"cluster after {waited / 60:.0f} min, so this task can "
                        f"never be scheduled. A worker has detached (the reverse "
                        f"SSH tunnels are the usual cause). Re-establish it with "
                        f"`bash scripts/red_env.sh up` and re-run."
                    ) from None
            if waited >= STAGE_MAX_SECONDS:
                raise RuntimeError(
                    f"{what}: still unfinished after {waited / 60:.0f} min "
                    f"(cap {STAGE_MAX_SECONDS / 60:.0f} min) — giving up rather "
                    f"than hanging."
                ) from None
            print(f"[red] still waiting on {what} ({waited / 60:.0f} min)")


def _dispatch_llm(llm, message: str, tools, tag: str = "agent",
                  log_path: str | None = None):
    """Dispatch one agent turn; None-safe (used by --local smoke runs).

    A failed turn (backend unreachable, tool servers down, repeated API
    errors) comes back as QueryResult(success=False) rather than raising —
    surface that loudly instead of letting the loop burn every critic round
    on a dead backend and report a bare "no ISASpec produced".

    The backend also writes its own transcript, but it writes it on whichever
    node ran the prompt — on a cluster that is the llm node, where the head
    never sees it. ``QueryResult.stream_result`` carries the same transcript
    back with the result, so append it here and the audit trail lands with the
    rest of the run's artifacts."""
    if llm is None:
        return None
    # Hand the backend relay-aware stand-ins: the head's tool ports are reachable
    # from a tunnelled worker only through CHIA_TOOL_RELAY_HOST (see RelayTool).
    #
    # The backend re-raises some failures instead of reporting them (a reply that
    # hits max_output_tokens, a content block, an auth error), and one of those
    # escaping would abandon a run that is twenty minutes in with every artifact
    # so far unsummarised. Turn them into a failed turn, which every caller
    # already knows how to handle: repair rounds, budget caps, escalation.
    try:
        res = _await(llm.prompt.options(resources={"llm": LLM_RESOURCE})
                     .chia_remote(llm, message, [RelayTool(t) for t in tools]),
                     f"{tag} LLM turn", RES_LLM)
    except Exception as exc:                                  # noqa: BLE001
        name = type(exc).__name__
        print(f"[red] {tag} LLM turn RAISED {name}: {str(exc)[:200]}")
        res = QueryResult(result="", returncode=-1, stderr=f"{name}: {exc}",
                          stream_result="", success=False)
    if res is not None and not getattr(res, "success", True):
        print(f"[red] WARNING: {tag} LLM turn FAILED "
              f"(returncode={getattr(res, 'returncode', '?')}) — the backend is "
              "unreachable or erroring; see the ray logs for VertexGeminiLLM.prompt")
    if log_path and res is not None and getattr(res, "stream_result", ""):
        with open(log_path, "a") as f:
            f.write(f"\n{'=' * 78}\n### {tag}\n{'=' * 78}\n{res.stream_result}")
    return res


def _dispatch_async(llm, message: str, tag: str = "agent", tools=()):
    """Dispatch an agent turn without waiting for it.

    Used wherever turns are independent of each other: the four reviewers (which
    are toolless — their context is the prompt and their answer is JSON), and
    A1 mining alongside A0 core analysis, which read different things and can
    overlap. The caller creates every ref first and resolves them afterwards.
    Returns an ObjectRef, or None when there is no backend."""
    if llm is None:
        return None
    return (llm.prompt.options(resources={"llm": LLM_RESOURCE})
            .chia_remote(llm, message, [RelayTool(t) for t in tools]))


def _resolve(ref, tag: str, log_path: str | None = None):
    """Resolve a ref from :func:`_dispatch_async`, with the same failure
    handling as a blocking turn."""
    if ref is None:
        return None
    try:
        res = _await(ref, f"{tag} LLM turn", RES_LLM)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[red] {tag} LLM turn RAISED {type(exc).__name__}: {str(exc)[:200]}")
        return QueryResult(result="", returncode=-1, stderr=str(exc),
                           stream_result="", success=False)
    if res is not None and not getattr(res, "success", True):
        print(f"[red] WARNING: {tag} LLM turn FAILED")
    if log_path and res is not None and getattr(res, "stream_result", ""):
        with open(log_path, "a") as f:
            f.write(f"\n{'=' * 78}\n### {tag}\n{'=' * 78}\n{res.stream_result}")
    return res


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
        return _dispatch_llm(llm, message, iss_tools, tag=tag, log_path=log_path)
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
            profile = state_def.from_json(text[start:end + 1], state_def.CoreProfile)
            profile.name = profile.name or name
            state_def.validate_core_profile(profile)
            return profile
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"[red] A0: unusable core profile ({exc}); "
                  "the designer will work without it")
    return None


def _read_artifact(path: str, cls, fallback=None):
    if os.path.exists(path):
        try:
            return state_def.from_json(open(path).read(), cls)
        except (json.JSONDecodeError, TypeError, ValueError):
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
            items = json.load(f)
    except (json.JSONDecodeError, OSError):
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


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(state_def.to_json(obj))


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
                 llm=None, tools=None, local: bool = False,
                 harness: str = "") -> REDResult:
    """One end-to-end RED pass: A1 + Gate 1 -> HotLoopReport, then A2 + link 1
    (with a bounded repair edge) -> ISASpec."""
    tag = run_id.rsplit("-", 1)[-1]
    os.makedirs(work_root, exist_ok=True)
    profile_dir = os.path.join(work_root, "profile")
    llm_logs_dir = os.path.join(work_root, "llm_logs")
    for d in (profile_dir, llm_logs_dir):
        os.makedirs(d, exist_ok=True)

    core_path = os.path.join(work_root, "CoreProfile.json")
    profile_path = os.path.join(work_root, "profile.json")
    report_path = os.path.join(work_root, "HotLoopReport.json")
    spec_path = os.path.join(work_root, "ISASpec.json")
    status_path = os.path.join(work_root, "status.md")
    knowledge_path = os.path.join(work_root, "knowledge.md")
    sentinel_path = os.path.join(work_root, "finished")
    transcript_path = os.path.join(llm_logs_dir, "transcript.log")

    if tools is None:
        # One tool server, not seven. Every ChiaTool is its own Ray actor and
        # each rescans the port range as it binds, so a server per artifact cost
        # minutes before the first agent turn (see red/tools.py). In --local
        # smoke mode nothing advertises head_local, so placement is left default.
        task_opts = None if local else HEAD_LOCAL
        tools = [DesignerTool(
            name=f"red_dsn_{tag}", source_roots=[project, core],
            profile_path=profile_path, report_path=report_path,
            spec_path=spec_path, status_path=status_path,
            knowledge_path=knowledge_path, sentinel_path=sentinel_path,
            core_path=core_path, workload=workload, task_options=task_opts)]
    finish_tool = next((t for t in tools if hasattr(t, "was_finished")), None)
    if finish_tool is not None:
        finish_tool.reset()
    tm = _tool_map(tools)

    def _mirror(rel: str, content) -> None:
        """Archive one artifact to the database node AND beside the run.

        The local copy is not redundancy for its own sake: every per-round
        review and performance verdict used to live only on the database node,
        so tearing the cluster down after a run destroyed the evidence needed to
        explain why it had not converged.
        """
        try:
            here = os.path.join(work_root, rel)
            os.makedirs(os.path.dirname(here), exist_ok=True)
            with open(here, "w") as f:
                f.write(content if isinstance(content, str) else str(content))
        except OSError as exc:
            print(f"[red] could not write local copy of {rel}: {exc}")
        if archive_dir:
            if local:
                db_node.put(archive_dir, rel, content, workload=workload)
            else:
                # Same reason as the transcript archival below: a bare get()
                # against a node that has left the cluster never returns, and
                # this one is called from every stage.
                try:
                    _await(db_node.put.chia_remote(archive_dir, rel, content,
                                                   workload=workload),
                           f"archiving {rel}", RES_DATABASE)
                except RuntimeError as exc:
                    print(f"[red] could not archive {rel} to the store: {exc}")

    # The persistent knowledge graph (red/graph.py). Opened here so a run is
    # recorded from its first stage; every call is a no-op when Neo4j is not
    # running, so a design run never depends on it.
    run_started = time.monotonic()
    graph.start_run(workload, run_id, project, core, model=LLM_MODEL,
                    harness=harness, cflags=cflags)

    gate1, gate2 = None, None
    iss_tools: list = []          # built lazily when Gate 2's implementer runs
    sec_tools: list = []          # built lazily when Gate 4's reviewer runs
    chain: list[state_def.GateResult] = []
    report, spec = None, None
    iters = 0
    try:
        # ---------------- A1: profile x2 -> Gate 1 -> mining agent ----------
        prof_args = (project, workload, profile_dir, cflags, harness)
        if local:
            prof1 = nodes.A1_profile_workload(*prof_args)
            prof2 = nodes.A1_profile_workload(*prof_args)
        else:
            # Gate 1 needs two independent profiles; dispatch both before
            # resolving either so the second run is free in wall-clock terms.
            # They write to sibling directories so they cannot share a harness.
            refs = [nodes.A1_profile_workload.chia_remote(
                        project, workload, os.path.join(profile_dir, f"run{i}"),
                        cflags, harness)
                    for i in (1, 2)]
            # Resolve one at a time: both tasks are already running, so this
            # still costs one profile's wall clock, and get() unwraps the
            # profiler's result wrapper per ref (it does not for a list).
            prof1, prof2 = [_await(r, "A1 profile", RES_PROFILE) for r in refs]
        _write_json(profile_path, prof1)
        _mirror("profile.json", state_def.to_json(prof1))

        total_a = sum(v for k, v in prof1.get("cycle_counts", {}).items() if k != "__total__")
        total_b = sum(v for k, v in prof2.get("cycle_counts", {}).items() if k != "__total__")
        gate1 = nodes.gate1(total_a, total_b) if local \
            else _await(nodes.gate1.chia_remote(total_a, total_b),
                        "Gate 1", RES_HEAD_LOCAL)
        _mirror("gates/gate1.json", state_def.to_json(gate1))

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
        mine_ref = _dispatch_async(llm, mine_msg, "A1 mining", tools)
        core_ref = _dispatch_async(llm, core_msg, "A0 core analysis", tools)
        _resolve(mine_ref, "A1 mining", transcript_path)
        core_res = _resolve(core_ref, "A0 core analysis", transcript_path)

        core_profile = _parse_core_profile(core, core_res)
        graph.record_core_profile(core, core_profile)
        if core_profile is not None:
            _write_json(core_path, core_profile)
            _mirror("CoreProfile.json", state_def.to_json(core_profile))
            print(f"[{run_id}] A0: {core_profile.name} {core_profile.isa}, "
                  f"{core_profile.coprocessor} "
                  f"(memory access: {core_profile.coprocessor_memory_access}), "
                  f"{len(core_profile.constraints)} constraint(s)")
        report = _read_artifact(report_path, state_def.HotLoopReport) \
            or build_mechanical_report(project, workload, prof1)
        report = _merge_mechanical(report, prof1,
                                   source_roots=[project, core])
        report.passes_gate1 = gate1.passed
        _write_json(report_path, report)
        _mirror("HotLoopReport.json", state_def.to_json(report))
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
                GRAPH_PRIOR_DESIGNS=tm.get("graph_prior_designs", ""),
                GRAPH_PRIOR_FINDINGS=tm.get("graph_prior_findings", ""),
                GRAPH_BEST_DESIGN=tm.get("graph_best_design", ""),
                GRAPH_LOOPS=tm.get("graph_loops", ""),
                GRAPH_QUERY=tm.get("graph_query", ""),
                CORE_PROFILE=review.render_core(core_profile),
                CRITIC_MAX_ROUNDS=str(CRITIC_MAX_ROUNDS))
            # NOTE: Gemini has no cross-call session resume, so each round
            # re-supplies context via the read tools (see red/prompts/system.md).
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
                                    log_path=transcript_path)
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
            return REDResult(workload, False, iters, gate1=gate1, gate2=gate2,
                             chain=chain, report=report, spec=spec)

        spec.critic_rounds = min(iters, CRITIC_MAX_ROUNDS)
        # link-1 status is the loop's to record, never the agent's to claim.
        spec.passes_link1 = False
        spec.link1_vectors = 0
        _write_json(spec_path, spec)
        _mirror("ISASpec.draft.json", state_def.to_json(spec))
        report_json = state_def.to_json(report)
        spec_json = state_def.to_json(spec)

        # ---------------- link 1, with a bounded agentic repair edge --------
        link1_dir = os.path.join(work_root, "link1")

        def _run_link1(sjson: str) -> state_def.GateResult:
            return (nodes.link1(report_json, sjson, link1_dir) if local
                    else _await(nodes.link1.chia_remote(report_json, sjson, link1_dir),
                                "link 1", RES_PROFILE))

        l1 = _run_link1(spec_json)
        # A failing link 1 is a concrete counterexample (a compile error, a
        # sanitizer report, a non-pure model). Hand it back to the agent through
        # the sealed status tool and let it revise — capped, then escalate.
        for repair in range(1, REPAIR_MAX_ROUNDS + 1):
            if l1.passed or llm is None:
                break
            print(f"[{run_id}] link1 FAILED — repair round {repair}/{REPAIR_MAX_ROUNDS}")
            _publish_status(status_path, l1, f"link 1 repair round {repair}")
            _mirror(f"gates/link1.repair{repair}.json", state_def.to_json(l1))
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
                tools, tag=f"link1 repair {repair}", log_path=transcript_path)
            revised = _read_artifact(spec_path, state_def.ISASpec)
            if revised is None:
                break
            spec = revised
            spec_json = state_def.to_json(spec)
            l1 = _run_link1(spec_json)

        # ---------------- Gate 3: is it actually faster? --------------------
        # The gates above establish that an instruction means what it says.
        # This one asks what it costs. RED shipped a verified extension that was
        # 2% slower than the baseline at +74% area (eval/RESULTS.md) because
        # nothing ever asked; red/cost.py is that question, calibrated against
        # the RTL those numbers came from. A design that cannot beat the
        # software it replaces goes back to the designer with the arithmetic.
        perf = None
        plat = cost.Platform.from_core(core_profile)
        # A redesign is not guaranteed to be an improvement — one run went
        # 1.64x, then 0.96x, then 8.33x. Keep the best spec seen so the loop
        # never hands on a design worse than one it already had.
        best_spec_json, best_perf = None, None
        if llm is not None:
            for round_ in range(1, PERF_MAX_ROUNDS + 1):
                # Measure what each C model really does before costing it:
                # `mac_ops` is the designer's own claim about the price of its
                # design, and a review round caught a spec declaring 0 for a
                # 512-iteration reduction.
                measured = cost.measure_ops(spec, os.path.join(work_root, "gate3"))
                perf = cost.estimate(spec, report, core_profile, measured)
                _mirror(f"perf/round{round_}.json", state_def.to_json(perf))
                graph.record_spec(workload, run_id, spec, stage=f"gate3.{round_}")
                graph.record_perf(workload, run_id, perf, round_)
                _mirror(f"perf/round{round_}.md",
                        cost.render_feedback(perf, platform=plat))
                if perf.measurement_error:
                    # The profile, not the design, is unusable. Redesigning
                    # cannot help, and pretending otherwise burns the budget
                    # and reports a bad extension instead of a broken profiler.
                    print(f"[{run_id}] Gate 3 cannot score any design: "
                          f"{perf.measurement_error}")
                    break
                regressed = (best_perf is not None
                             and perf.app_speedup < best_perf.app_speedup)
                print(f"[{run_id}] Gate 3 round {round_}: predicted "
                      f"{perf.app_speedup:.2f}x over {perf.covered_share:.0%} of "
                      f"cycles (target {SPEEDUP_TARGET:.2f}x)"
                      + (f" — worse than round {round_ - 1}'s "
                         f"{best_perf.app_speedup:.2f}x" if regressed else ""))
                if best_perf is None or perf.app_speedup > best_perf.app_speedup:
                    best_spec_json, best_perf = spec_json, perf
                if perf.meets_target():
                    break
                if round_ == PERF_MAX_ROUNDS:
                    # Hand on the best design, not the most recent one.
                    if best_spec_json is not None and best_perf is not perf:
                        print(f"[{run_id}] Gate 3 budget spent; keeping round "
                              f"{best_perf.app_speedup:.2f}x over the final "
                              f"{perf.app_speedup:.2f}x")
                        spec = state_def.from_json(best_spec_json, state_def.ISASpec)
                        spec_json = best_spec_json
                        perf = best_perf
                        _write_json(spec_path, spec)
                        l1 = _run_link1(spec_json)
                    print(f"[{run_id}] Gate 3 budget spent at "
                          f"{perf.app_speedup:.2f}x — escalating")
                    break
                with open(status_path, "w") as f:
                    f.write(cost.render_feedback(perf, platform=plat))
                iters += 1
                _dispatch_llm(llm, _load_prompt(
                    "redesign.md",
                    READ_SPEC=tm.get("read_spec", ""), READ_REPORT=tm.get("read_report", ""),
                    READ_STATUS=tm.get("read_status", ""), WRITE_SPEC=tm.get("write_spec", ""),
                    APPEND_KNOWLEDGE=tm.get("append_knowledge", ""),
                    FINISH=tm.get("finish", ""),
                    TARGET=f"{SPEEDUP_TARGET:.2f}",
                    VERDICT=(cost.render_feedback(perf, platform=plat)
                             + (f"\n**Your last change made this worse** — the "
                                f"previous design reached "
                                f"{best_perf.app_speedup:.2f}x. Reconsider it "
                                f"rather than continuing in this direction.\n"
                                if regressed else ""))),
                    tools, tag=f"Gate 3 redesign {round_}", log_path=transcript_path)
                revised = _read_artifact(spec_path, state_def.ISASpec)
                if revised is None:
                    break
                spec = revised
                spec_json = state_def.to_json(spec)
                l1 = _run_link1(spec_json)
                _mirror(f"gates/link1.perf{round_}.json", state_def.to_json(l1))
            gate3 = state_def.GateResult(
                gate="gate3", passed=bool(perf and perf.meets_target()),
                detail=(f"predicted {perf.app_speedup:.2f}x over "
                        f"{perf.covered_share:.0%} of cycles "
                        f"(target {SPEEDUP_TARGET:.2f}x)" if perf else "not run"),
                counterexample=None if (perf and perf.meets_target())
                else (perf.detail if perf else None))
            _mirror("gates/gate3.json", state_def.to_json(gate3))

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
        agent_findings_path = os.path.join(sec_dir, "agent_findings.json")
        core_json = state_def.to_json(core_profile) if core_profile else ""
        os.makedirs(sec_dir, exist_ok=True)
        security_revisions = 0

        def _scan(sjson: str, where: str) -> state_def.SecurityReport:
            raw = (nodes.security_scan(sjson, core_json, where) if local
                   else _await(nodes.security_scan.chia_remote(sjson, core_json,
                                                               where),
                               "security scan", RES_PROFILE))
            return state_def.from_json(raw, state_def.SecurityReport)

        if llm is not None:
            for round_ in range(1, SECURITY_MAX_ROUNDS + 1):
                sec_report = _scan(spec_json,
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
                        knowledge_path=knowledge_path,
                        task_options=None if local else HEAD_LOCAL))
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
                        sec_tools, tag=f"security agent {round_}",
                        log_path=transcript_path)
                finally:
                    llm.system_message = designer_charter
                sec_report = security.merge(sec_report,
                                            _read_findings(agent_findings_path))
                gate4 = security.verdict(sec_report)
                _mirror(f"security/round{round_}.json",
                        state_def.to_json(sec_report))
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
                    tools, tag=f"security revision {round_}",
                    log_path=transcript_path)
                revised = _read_artifact(spec_path, state_def.ISASpec)
                if revised is None:
                    break
                spec = revised
                spec_json = state_def.to_json(spec)
                # The models changed, so their meaning has to be re-established
                # before anything downstream trusts them again.
                l1 = _run_link1(spec_json)
                _mirror(f"gates/link1.security{round_}.json",
                        state_def.to_json(l1))

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
                _mirror("gates/gate3.json", state_def.to_json(gate3))
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
        # two cluster runs walked their blocking findings down to 1 and then
        # regressed on the final round (1 -> 4 and 1 -> 3, one of them also
        # breaking a C model so link 1 fell to 1/2), and the loop reported that
        # regression because it handed on the *latest* spec instead of the best
        # one. Ranked by blocking findings first, then link 1, then total
        # findings -- never by anything the designer asserts about itself.
        best_review = None       # (rank, spec_json, review_report, l1)
        if llm is not None:
            for round_ in range(1, REVIEW_MAX_ROUNDS + 1):
                # Clear the A2 charter first: a reviewer told it is the designer
                # reviews its own work. Each reviewer's whole brief is its
                # prompt, and llm is pickled per dispatch, so this is enough to
                # keep the roles apart.
                designer_charter, llm.system_message = llm.system_message, ""
                try:
                    review_report = review.review_spec(
                        llm, spec_json, report_json, core,
                        dispatch=lambda msg, tag_: _dispatch_async(llm, msg, tag_),
                        core_profile=core_profile, prior=review_report,
                        perf=perf, security=sec_report)
                finally:
                    llm.system_message = designer_charter
                _mirror(f"review/round{round_}.json",
                        state_def.to_json(review_report))
                graph.record_spec(workload, run_id, spec, stage=f"review.{round_}")
                graph.record_review(workload, run_id, review_report, round_)
                _mirror(f"review/round{round_}.md", review.render(review_report))
                n_block = review_report.blocking()
                rank = _review_rank(review_report, l1)
                regressed = best_review is not None and rank > best_review[0]
                print(f"[{run_id}] review round {round_}: "
                      f"{len(review_report.findings)} finding(s), {n_block} blocking "
                      f"({', '.join(review_report.reviewers) or 'no reviewer replied'})"
                      + (f" — worse than the best so far "
                         f"({best_review[2].blocking()} blocking)" if regressed else ""))
                if best_review is None or rank < best_review[0]:
                    best_review = (rank, spec_json, review_report, l1)
                if not n_block:
                    break
                if round_ == REVIEW_MAX_ROUNDS:
                    # Hand on the best spec the review reached, not the last one.
                    if best_review is not None and best_review[2] is not review_report:
                        print(f"[{run_id}] review budget spent; keeping the round "
                              f"with {best_review[2].blocking()} blocking finding(s) "
                              f"over the final {n_block}")
                        spec_json = best_review[1]
                        spec = state_def.from_json(spec_json, state_def.ISASpec)
                        review_report = best_review[2]
                        _write_json(spec_path, spec)
                        l1 = _run_link1(spec_json)
                        _mirror("gates/link1.review_best.json", state_def.to_json(l1))
                        n_block = review_report.blocking()
                    print(f"[{run_id}] review budget spent with {n_block} blocking "
                          "finding(s) outstanding — escalating to the summary")
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
                    FINDINGS=review.render(review_report)),
                    tools, tag=f"designer revision {round_}", log_path=transcript_path)
                revised = _read_artifact(spec_path, state_def.ISASpec)
                if revised is None:
                    break
                spec = revised
                spec_json = state_def.to_json(spec)
                # The spec changed under it, so link 1 must be re-established
                # before the revised models can be trusted.
                l1 = _run_link1(spec_json)
                _mirror(f"gates/link1.review{round_}.json", state_def.to_json(l1))

        # ---------------- Gate 2: the ISS implementer, then the diff --------
        gate2_dir = os.path.join(work_root, "gate2")
        # The implementer gets its own sealed tools: a spec view with the C
        # models hidden (IssSpecTool), the last verdict, and the shared notes.
        if llm is not None and not iss_tools:
            opts = None if local else HEAD_LOCAL
            iss_tools.append(IssTool(
                name=f"red_iss_{tag}", spec_path=spec_path,
                status_path=status_path, knowledge_path=knowledge_path,
                task_options=opts))

        def _run_gate2(sjson: str) -> state_def.GateResult:
            return (nodes.gate2(sjson, gate2_dir) if local
                    else _await(nodes.gate2.chia_remote(sjson, gate2_dir),
                                "Gate 2", RES_SPIKE))

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
            spec_json = state_def.to_json(spec)
            gate2 = _run_gate2(spec_json)
            if gate2.passed or llm is None or attempt == REPAIR_MAX_ROUNDS:
                break
            # A divergence means the two implementers read the same words
            # differently: the spec was ambiguous. Send it back to be sharpened.
            print(f"[{run_id}] gate2 FAILED — repair round "
                  f"{attempt + 1}/{REPAIR_MAX_ROUNDS}: {gate2.detail}")
            _mirror(f"gates/gate2.repair{attempt + 1}.json", state_def.to_json(gate2))
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
                tools, tag=f"gate2 repair {attempt + 1}")
            spec = _read_artifact(spec_path, state_def.ISASpec) or spec
            # The spec changed, so the previous Spike models are stale — the
            # next round re-implements them from the revised wording.
            for ins in spec.instructions:
                ins.spike_model = ""
            _write_json(spec_path, spec)

        if gate2 is None:
            gate2 = _run_gate2(state_def.to_json(spec))
        l2 = nodes.link2(state_def.to_json(gate2)) if local \
            else _await(nodes.link2.chia_remote(state_def.to_json(gate2)),
                        "link 2", RES_HEAD_LOCAL)
        _mirror("gates/gate2.json", state_def.to_json(gate2))

        chain = [l1, l2]
        _mirror("gates/chain.json", state_def.to_json(chain))

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
        _write_json(spec_path, spec)
        _mirror("ISASpec.json", state_def.to_json(spec))

        # Gate 4, on the spec that actually ships. The review may have revised
        # the design after the security stage passed it, and a revision made for
        # one reviewer can reintroduce exactly what the security gate rejected —
        # a conditional subtraction put back to save a few cycles, an output
        # word dropped to narrow the block. So the final artifact is re-checked
        # mechanically. No agent runs here: it is the batteries alone, it costs
        # a compile and a few hundred vectors per model, and what it is guarding
        # is the only version anyone will ever build.
        if llm is not None:
            final_sec = _scan(state_def.to_json(spec),
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
            sec_report = final_sec
            _mirror("security/final.json", state_def.to_json(sec_report))
            _mirror("security/final.md", security.render(sec_report))
            _mirror("gates/gate4.json", state_def.to_json(gate4))
            graph.record_security(workload, run_id, sec_report, 0)

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
        for g in [gate2, gate3, gate4] + list(chain):
            graph.record_gate(workload, run_id, g)
        graph.finish_run(
            workload, run_id, converged=converged,
            wallclock=time.monotonic() - run_started, blocking=blocking,
            speedup=(perf.app_speedup if perf else 0.0))

        return REDResult(workload, converged, iters, gate1=gate1, gate2=gate2,
                         gate3=gate3, gate4=gate4, chain=chain, report=report,
                         spec=spec, review=review_report, security=sec_report)
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
                if local:
                    db_node.archive_dir(archive_dir, "llm_logs", blob, workload=workload)
                else:
                    # Through the watchdog, not a bare get(). This runs in the
                    # `finally`, so it runs precisely when something has already
                    # gone wrong -- including "the database node left the
                    # cluster", which is exactly when a bare get() waits
                    # forever. One run wedged here for 33 minutes after its llm
                    # and database workers detached, with the instances still
                    # billing, while the loop had already finished its work.
                    _await(db_node.archive_dir.chia_remote(
                        archive_dir, "llm_logs", blob, workload=workload),
                        "transcript archival", RES_DATABASE)
        except Exception as exc:  # noqa: BLE001 — archival must not fail the run
            print(f"[{run_id}] WARNING: could not archive LLM transcripts: {exc}")
        for t in list(tools) + iss_tools + sec_tools:
            t.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _render_summary(r: REDResult, run_n: int) -> str:
    def mark(g):
        return "PASS" if g and g.passed else "FAIL" if g else "—"
    chain_lines = "\n".join(f"- {mark(g)} {g.gate}: {g.detail}" for g in r.chain)
    coverage = f"{r.report.coverage:.2%} ({COVERAGE_TARGET:.0%} target)" if r.report else "n/a"
    instrs = "\n".join(
        f"  - `{i.mnemonic}` ({i.opcode}, {i.words}x32b): {i.semantics}"
        for i in (r.spec.instructions if r.spec else []))
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
    return (f"# RED run_{run_n} — {r.workload}\n\n"
            f"- model: `{LLM_BACKEND}:{LLM_MODEL}` (project `{GCP_PROJECT}`)\n"
            f"- result: **{'converged (A1+A2)' if r.converged else 'NOT converged'}**\n"
            f"- A2 rounds: {r.iterations}\n"
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
            f"({len(r.spec.instructions) if r.spec else 0} instructions)\n{instrs}\n")


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
    p.add_argument("--run-id", default=None)
    p.add_argument("--work-root", default=None)
    p.add_argument("--local", action="store_true",
                   help="run nodes in-process and skip the LLM (mechanical smoke test)")
    p.add_argument("--local-llm", action="store_true",
                   help="run the full A1+A2 loop on this machine with the real "
                        "Gemini backend (no cluster; needs ADC on this host)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if args.local and args.local_llm:
        raise SystemExit("--local and --local-llm are mutually exclusive")
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"{ts}-{uuid.uuid4().hex[:6]}"
    work_root = args.work_root or os.path.join(RED_LOG_ROOT, args.workload, run_id)
    single_machine = args.local or args.local_llm

    import ray
    runtime_env = {"working_dir": REPO_ROOT,
                   "py_modules": [os.path.join(REPO_ROOT, "red")]}
    if single_machine:
        # Advertise the cluster's resource tags locally so the same dispatch
        # path (chia_remote) schedules on this one node.
        ray.init(runtime_env=runtime_env, resources=LOCAL_RESOURCES)
    else:
        ray.init(address="auto", runtime_env=runtime_env)  # the CHIA cluster from cluster.yaml
    start_collector(log_dir=os.path.join(work_root, "profiler"))

    llm = None
    if not args.local:
        if LLM_BACKEND != "gemini":
            raise SystemExit(f"unsupported LLM_BACKEND {LLM_BACKEND!r} (only 'gemini' is wired)")
        # No log_dir: the backend would write its transcript on whichever node
        # runs the prompt, and on a cluster that path does not exist there
        # (output/ is git-ignored, so it is never rsynced) — the write then
        # raises inside the tool task group and every turn fails. The driver
        # captures QueryResult.stream_result on the head instead
        # (see _dispatch_llm).
        llm = VertexGeminiLLM(
            model=LLM_MODEL, project=GCP_PROJECT, location=GCP_LOCATION,
            # Pass the output budget explicitly. It was imported and never
            # handed to the backend, so every turn ran at VertexGeminiLLM's
            # 16,000-token default -- and a reply carrying two C models, or the
            # ISS agent's two Spike models, does not fit in 16k. It arrives as
            # MaxOutputTokensError, which the loop treats as a failed turn, so
            # the symptom was "the backend is erroring" rather than "the reply
            # was too long". Three runs in one batch died this way.
            max_tokens=LLM_MAX_TOKENS,
            timeout_seconds=LLM_TIMEOUT_SECONDS)

    run_n, sweep_path = (db_node.claim_run(args.workload, DB_ROOT) if args.local
                         else get(db_node.claim_run.chia_remote(args.workload, DB_ROOT)))
    try:
        result = run_red_loop(args.project, args.core, args.workload, args.cflags,
                              run_id, work_root, archive_dir=sweep_path,
                              llm=llm, tools=([] if args.local else None),
                              local=args.local, harness=args.harness)
    except Exception:
        import traceback
        traceback.print_exc()
        result = REDResult(args.workload, False, 0)

    summary = _render_summary(result, run_n)
    if args.local:
        db_node.write_text(sweep_path, "summary.md", summary, workload=args.workload)
    else:
        get(db_node.write_text.chia_remote(sweep_path, "summary.md", summary,
                                           workload=args.workload))

    # On a cloud cluster the store sits on a VM that teardown deletes — pull the
    # run back to the head so the artifacts survive `red_env.sh down`.
    local_sweep = sweep_path
    if not single_machine:
        # The database node numbers runs against ITS OWN store, which is a
        # different sequence from the head's — claim a free local slot instead of
        # reusing run_n, or a fetch would land on top of an earlier local run.
        try:
            _, local_sweep = db_node.claim_run(args.workload, DB_ROOT)
            blob = get(db_node.fetch_run.chia_remote(sweep_path))
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r") as tar:
                tar.extractall(path=local_sweep)
        except Exception as exc:  # noqa: BLE001 — the run itself already succeeded
            print(f"[red] WARNING: could not fetch the run from the database node "
                  f"({exc}); artifacts remain on that node at {sweep_path}")
            local_sweep = sweep_path
    print(f"\n{summary}\nartifacts -> {local_sweep}")
    stop_collector()
    return 0 if result.converged else 1


if __name__ == "__main__":
    raise SystemExit(main())
