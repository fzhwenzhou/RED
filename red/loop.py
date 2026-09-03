"""RED loop driver — orchestrate A1 kernel mining + A2 ISA synthesis.

A1 and A2 are the research contribution. Each is an *agentic* stage (a Gemini
agent over the sealed MCP tools) bounded by a *programmatic* gate; A3-A5 are the
fixed tool nodes dispatched from :mod:`red.nodes`. The loop:

    A1  profile the project (mechanical) x2  ->  Gate 1 (±5% re-profile)
        mining agent ranks hot loops         ->  HotLoopReport
    A2  synthesis agent (S2a->S2b->S2c)      ->  ISASpec
        Gate 2 (C <=> Spike, 10^5)  +  link1 (kernel<=>C) + link2 (C<=>Spike)
    A3-A5 fixed tool nodes  ->  link3 (Spike<=>RTL) + link4 (RTL<=>e2e)
    verify_chain assembles the four links.

Every artifact is persisted as typed JSON (HotLoopReport / ISASpec) and
mirrored to the durable DB sweep as it is produced.

Two run modes:
    default      dispatch nodes via .chia_remote() onto the CHIA cluster
    --local      call nodes in-process (no cluster resources) and skip the LLM
                 (llm=None) so the mechanical A1 + gates smoke-test on one
                 machine without credentials.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
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
    EXAMPLE_CORE,
    EXAMPLE_PROJECT,
    EXAMPLE_WORKLOAD,
    GCP_LOCATION,
    GCP_PROJECT,
    LLM_BACKEND,
    LLM_MODEL,
    LLM_PROMPTS_DIR,
    LLM_RESOURCE,
    LLM_TIMEOUT_SECONDS,
    MAX_ITERATIONS,
    RED_LOG_ROOT,
    REPO_ROOT,
)
from red.tools import (
    FinishTool,
    KnowledgeTool,
    ProfileTool,
    ReportTool,
    SourceTool,
    SpecTool,
    StatusTool,
)

HEAD_LOCAL = {"resources": {"head_local": 0.1}}   # sealed-tool placement


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


def _dispatch_llm(llm, message: str, tools, tag: str = "agent"):
    """Dispatch one agent turn; None-safe (used by --local smoke runs).

    A failed turn (backend unreachable, tool servers down, repeated API
    errors) comes back as QueryResult(success=False) rather than raising —
    surface that loudly instead of letting the loop burn every critic round
    on a dead backend and report a bare "no ISASpec produced"."""
    if llm is None:
        return None
    res = get(llm.prompt.options(resources={"llm": LLM_RESOURCE})
              .chia_remote(llm, message, tools))
    if res is not None and not getattr(res, "success", True):
        print(f"[red] WARNING: {tag} LLM turn FAILED "
              f"(returncode={getattr(res, 'returncode', '?')}) — the backend is "
              "unreachable or erroring; see the ray logs for VertexGeminiLLM.prompt")
    return res


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
                 llm=None, tools=None, local: bool = False) -> REDResult:
    """One end-to-end RED pass: A1 + Gate 1, A2 + Gate 2 + links 1-2, then the
    A3-A5 fixed nodes feeding links 3-4."""
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
    chain: list[state_def.GateResult] = []
    report, spec = None, None
    iters = 0
    try:
        # ---------------- A1: profile x2 -> Gate 1 -> mining agent ----------
        prof_args = (project, workload, profile_dir, cflags)
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
        _dispatch_llm(llm, mine_msg, tools, tag="A1 mining")
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
                res = _dispatch_llm(llm, msg, tools, tag=f"A2 round {r}")
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
        _write_json(spec_path, spec)
        _mirror("ISASpec.json", state_def.to_json(spec))
        report_json = state_def.to_json(report)
        spec_json = state_def.to_json(spec)

        gate2 = nodes.gate2(spec_json) if local else get(nodes.gate2.chia_remote(spec_json))
        _mirror("gates/gate2.json", state_def.to_json(gate2))
        l1 = nodes.link1(report_json, spec_json) if local \
            else get(nodes.link1.chia_remote(report_json, spec_json))
        l2 = nodes.link2(state_def.to_json(gate2)) if local \
            else get(nodes.link2.chia_remote(state_def.to_json(gate2)))

        # ---------------- A3-A5 fixed tool nodes -> links 3-4 ---------------
        a3 = nodes.A3_eval_rtl(core, spec_json) if local \
            else get(nodes.A3_eval_rtl.chia_remote(core, spec_json))
        a5 = nodes.A5_eval_e2e(spec_json, core) if local \
            else get(nodes.A5_eval_e2e.chia_remote(spec_json, core))
        _mirror("evals/A3.json", state_def.to_json(a3))
        _mirror("evals/A5.json", state_def.to_json(a5))

        chain = [l1, l2,
                 state_def.GateResult(gate="link3", passed=a3.passed, detail=a3.detail),
                 state_def.GateResult(gate="link4", passed=a5.passed, detail=a5.detail)]
        _mirror("gates/chain.json", state_def.to_json(chain))

        links_12 = [l1, l2]
        converged = gate1.passed and all(g.passed for g in links_12)
        return REDResult(workload, converged, iters, gate1=gate1, gate2=gate2,
                         chain=chain, report=report, spec=spec)
    finally:
        for t in tools:
            t.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _render_summary(r: REDResult, run_n: int) -> str:
    def mark(g):
        return "✓" if g and g.passed else "✗" if g else "—"
    chain_lines = "\n".join(f"- {mark(g)} {g.gate}: {g.detail}" for g in r.chain)
    coverage = f"{r.report.coverage:.2%} ({COVERAGE_TARGET:.0%} target)" if r.report else "n/a"
    return (f"# RED run_{run_n} — {r.workload}\n\n"
            f"- model: `{LLM_BACKEND}:{LLM_MODEL}` (project `{GCP_PROJECT}`)\n"
            f"- result: **{'✓ converged (A1+A2)' if r.converged else '✗ not converged'}**\n"
            f"- iterations: {r.iterations}\n"
            f"- Gate 1: {mark(r.gate1)} {r.gate1.detail if r.gate1 else ''}\n"
            f"- Gate 2: {mark(r.gate2)} {r.gate2.detail if r.gate2 else ''}\n"
            f"- coverage: {coverage}\n"
            f"- verification chain:\n{chain_lines}\n")


def _parse_args():
    p = argparse.ArgumentParser(description="RED loop — A1 kernel mining + A2 ISA synthesis.")
    p.add_argument("--project", default=EXAMPLE_PROJECT,
                   help="application project dir (sources + workloads)")
    p.add_argument("--core", default=EXAMPLE_CORE, help="processor core dir (Verilog)")
    p.add_argument("--workload", default=EXAMPLE_WORKLOAD, help="workload name")
    # Compile input for the bundled example harness (curve + word-size defines);
    # any other project passes its own build flags here.
    p.add_argument("--cflags", default="-DuECC_VLI_N_BYTES=32 -DuECC_CURVE=uECC_secp256r1 -DuECC_WORD_SIZE=8",
                   help="cc flags for the harness build (example default)")
    p.add_argument("--run-id", default=None)
    p.add_argument("--work-root", default=None)
    p.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    p.add_argument("--local", action="store_true",
                   help="run nodes in-process and skip the LLM (smoke test)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"{ts}-{uuid.uuid4().hex[:6]}"
    work_root = args.work_root or os.path.join(RED_LOG_ROOT, args.workload, run_id)

    import ray
    runtime_env = {"working_dir": REPO_ROOT,
                   "py_modules": [os.path.join(REPO_ROOT, "red")]}
    if args.local:
        ray.init(runtime_env=runtime_env)                 # single local node, no resources
    else:
        ray.init(address="auto", runtime_env=runtime_env)  # the CHIA cluster from cluster.yaml
    start_collector(log_dir=os.path.join(work_root, "profiler"))

    llm = None
    if not args.local:
        if LLM_BACKEND != "gemini":
            raise SystemExit(f"unsupported LLM_BACKEND {LLM_BACKEND!r} (only 'gemini' is wired)")
        llm = VertexGeminiLLM(
            model=LLM_MODEL, project=GCP_PROJECT, location=GCP_LOCATION,
            timeout_seconds=LLM_TIMEOUT_SECONDS)

    run_n, sweep_path = (db_node.claim_run(args.workload) if args.local
                         else get(db_node.claim_run.chia_remote(args.workload)))
    try:
        result = run_red_loop(args.project, args.core, args.workload, args.cflags,
                              run_id, work_root, archive_dir=sweep_path,
                              llm=llm, tools=([] if args.local else None),
                              local=args.local)
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
    print(f"\n{result}  ->  {sweep_path}")
    stop_collector()
    return 0 if result.converged else 1


if __name__ == "__main__":
    raise SystemExit(main())
