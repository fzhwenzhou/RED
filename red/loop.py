"""RED loop driver — orchestrate A1 kernel mining + A2 ISA synthesis.

A1 and A2 are the research contribution and RED's whole scope; the **ISASpec is
the final artifact**. Each stage is *agentic* (a Gemini agent over the sealed
MCP tools) and bounded by a *programmatic* edge from :mod:`red.nodes`:

    A1  profile the project (mechanical) x2  ->  Gate 1 (±5% re-profile)
        mining agent ranks hot loops         ->  HotLoopReport
    A2  synthesis agent (S2a->S2b->S2c)      ->  ISASpec
        link1 (coverage + C models built and stress-run)
    ISS implementer agent (a separate agent, from the spec's words alone)
        gate2 / link2 (C model <=> patched Spike, 10^5 vectors)

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
import sys
import tarfile
import uuid
from dataclasses import dataclass, field

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from chia.base.ChiaFunction import get
from chia.models.vertex import VertexGeminiLLM
from chia.trace.profiler import start_collector, stop_collector

from red import db_node, nodes, state_def
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
    LLM_MODEL,
    LLM_PROMPTS_DIR,
    LLM_RESOURCE,
    LLM_TIMEOUT_SECONDS,
    RED_LOG_ROOT,
    REPAIR_MAX_ROUNDS,
    REPO_ROOT,
)
from red.tools import (
    FinishTool,
    IssSpecTool,
    KnowledgeTool,
    ProfileTool,
    RelayTool,
    ReportTool,
    SourceTool,
    SpecTool,
    StatusTool,
)

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
    chain: list[state_def.GateResult] = field(default_factory=list)
    report: state_def.HotLoopReport | None = None
    spec: state_def.ISASpec | None = None


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
    """
    methods = ("list_sources", "read_source", "read_profile",
               "read_report", "write_report", "read_spec", "write_spec",
               "read_status", "read_knowledge", "append_knowledge", "finish")
    names = {}
    for t in tools:
        for fn in methods:
            if hasattr(t, fn):
                names[fn] = f"{t.name}__{t.name}_{fn}"[:64]
    return names


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
    res = get(llm.prompt.options(resources={"llm": LLM_RESOURCE})
              .chia_remote(llm, message, [RelayTool(t) for t in tools]))
    if res is not None and not getattr(res, "success", True):
        print(f"[red] WARNING: {tag} LLM turn FAILED "
              f"(returncode={getattr(res, 'returncode', '?')}) — the backend is "
              "unreachable or erroring; see the ray logs for VertexGeminiLLM.prompt")
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


def _read_artifact(path: str, cls, fallback=None):
    if os.path.exists(path):
        try:
            return state_def.from_json(open(path).read(), cls)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return fallback


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(state_def.to_json(obj))


# ---------------------------------------------------------------------------
# A1 — kernel mining
# ---------------------------------------------------------------------------

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
                      profile: dict) -> state_def.HotLoopReport:
    """Overwrite the report's dynamic numbers from the mechanical profile — the
    agent selects/ranks loops; the loop supplies cycle share, coverage, runs."""
    counts = profile.get("cycle_counts") or {}
    total = sum(v for k, v in counts.items() if k != "__total__") or 0
    for loop in report.loops:
        fn = loop.function or loop.loop_id.split("::")[0]
        n = counts.get(fn, loop.dynamic_cycles)
        loop.dynamic_cycles = n
        loop.cycle_share = (n / total) if total else loop.cycle_share
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

    profile_path = os.path.join(work_root, "profile.json")
    report_path = os.path.join(work_root, "HotLoopReport.json")
    spec_path = os.path.join(work_root, "ISASpec.json")
    status_path = os.path.join(work_root, "status.md")
    knowledge_path = os.path.join(work_root, "knowledge.md")
    sentinel_path = os.path.join(work_root, "finished")
    transcript_path = os.path.join(llm_logs_dir, "transcript.log")

    if tools is None:
        # In --local smoke mode there is no cluster advertising the head_local
        # resource, so the tool servers are built with default placement (the
        # agents never run in that mode anyway).
        task_opts = None if local else HEAD_LOCAL
        source_tool = SourceTool(name=f"red_src_{tag}", source_roots=[project, core],
                                 task_options=task_opts)
        profile_tool = ProfileTool(name=f"red_prof_{tag}", profile_path=profile_path,
                                   task_options=task_opts)
        report_tool = ReportTool(name=f"red_report_{tag}", report_path=report_path,
                                 task_options=task_opts)
        spec_tool = SpecTool(name=f"red_spec_{tag}", spec_path=spec_path,
                             task_options=task_opts)
        status_tool = StatusTool(name=f"red_status_{tag}", status_path=status_path,
                                 task_options=task_opts)
        knowledge_tool = KnowledgeTool(name=f"red_know_{tag}", knowledge_path=knowledge_path,
                                       task_options=task_opts)
        finish_tool = FinishTool(name=f"red_finish_{tag}", sentinel_path=sentinel_path,
                                 task_options=task_opts)
        tools = [source_tool, profile_tool, report_tool, spec_tool,
                 status_tool, knowledge_tool, finish_tool]
    finish_tool = next((t for t in tools if isinstance(t, FinishTool)), None)
    if finish_tool is not None:
        finish_tool.reset()
    tm = _tool_map(tools)

    def _mirror(rel: str, content) -> None:
        if archive_dir:
            if local:
                db_node.put(archive_dir, rel, content, workload=workload)
            else:
                get(db_node.put.chia_remote(archive_dir, rel, content, workload=workload))

    gate1, gate2 = None, None
    iss_tools: list = []          # built lazily when Gate 2's implementer runs
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
            prof1 = get(nodes.A1_profile_workload.chia_remote(*prof_args))
            prof2 = get(nodes.A1_profile_workload.chia_remote(*prof_args))
        _write_json(profile_path, prof1)
        _mirror("profile.json", state_def.to_json(prof1))

        total_a = sum(v for k, v in prof1.get("cycle_counts", {}).items() if k != "__total__")
        total_b = sum(v for k, v in prof2.get("cycle_counts", {}).items() if k != "__total__")
        gate1 = nodes.gate1(total_a, total_b) if local \
            else get(nodes.gate1.chia_remote(total_a, total_b))
        _mirror("gates/gate1.json", state_def.to_json(gate1))

        mine_msg = _load_prompt(
            "mine.md",
            LIST_SOURCES=tm.get("list_sources", ""), READ_SOURCE=tm.get("read_source", ""),
            READ_PROFILE=tm.get("read_profile", ""), WRITE_REPORT=tm.get("write_report", ""))
        _dispatch_llm(llm, mine_msg, tools, tag="A1 mining",
                      log_path=transcript_path)
        report = _read_artifact(report_path, state_def.HotLoopReport) \
            or build_mechanical_report(project, workload, prof1)
        report = _merge_mechanical(report, prof1)
        report.passes_gate1 = gate1.passed
        _write_json(report_path, report)
        _mirror("HotLoopReport.json", state_def.to_json(report))

        # ---------------- A2: synthesis (S2a->S2b->S2c) -> Gate 2 + links ----
        if llm is not None:
            system = _load_prompt(
                "system.md",
                READ_REPORT=tm.get("read_report", ""), READ_PROFILE=tm.get("read_profile", ""),
                READ_STATUS=tm.get("read_status", ""), READ_KNOWLEDGE=tm.get("read_knowledge", ""),
                WRITE_SPEC=tm.get("write_spec", ""), FINISH=tm.get("finish", ""),
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
                    else get(nodes.link1.chia_remote(report_json, sjson, link1_dir)))

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

        # ---------------- Gate 2: the ISS implementer, then the diff --------
        gate2_dir = os.path.join(work_root, "gate2")
        # The implementer gets its own sealed tools: a spec view with the C
        # models hidden (IssSpecTool), the last verdict, and the shared notes.
        if llm is not None and not iss_tools:
            opts = None if local else HEAD_LOCAL
            iss_tools.extend([
                IssSpecTool(name=f"red_iss_{tag}", spec_path=spec_path, task_options=opts),
                StatusTool(name=f"red_istat_{tag}", status_path=status_path, task_options=opts),
                KnowledgeTool(name=f"red_iknow_{tag}", knowledge_path=knowledge_path,
                              task_options=opts),
            ])

        def _run_gate2(sjson: str) -> state_def.GateResult:
            return (nodes.gate2(sjson, gate2_dir) if local
                    else get(nodes.gate2.chia_remote(sjson, gate2_dir)))

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
            else get(nodes.link2.chia_remote(state_def.to_json(gate2)))
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

        converged = gate1.passed and l1.passed and gate2.passed
        return REDResult(workload, converged, iters, gate1=gate1, gate2=gate2,
                         chain=chain, report=report, spec=spec)
    finally:
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
                    get(db_node.archive_dir.chia_remote(archive_dir, "llm_logs", blob,
                                                        workload=workload))
        except Exception as exc:  # noqa: BLE001 — archival must not fail the run
            print(f"[{run_id}] WARNING: could not archive LLM transcripts: {exc}")
        for t in list(tools) + iss_tools:
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
    return (f"# RED run_{run_n} — {r.workload}\n\n"
            f"- model: `{LLM_BACKEND}:{LLM_MODEL}` (project `{GCP_PROJECT}`)\n"
            f"- result: **{'converged (A1+A2)' if r.converged else 'NOT converged'}**\n"
            f"- A2 rounds: {r.iterations}\n"
            f"- Gate 1 (re-profile ±5%): {mark(r.gate1)} "
            f"{r.gate1.detail if r.gate1 else ''}\n"
            f"- Gate 2 (C model vs Spike): {mark(r.gate2)} "
            f"{r.gate2.detail if r.gate2 else ''}\n"
            f"- coverage: {coverage}\n"
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
