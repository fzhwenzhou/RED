"""Environment verification for RED.

Confirms the repository is set up corresponding to the design's requirements:
imports resolve, the toolchain matches each node's expectations, typed artifacts
round-trip through JSON (nested dataclasses included), the mechanical nodes
(gates, links, A3-A5) run, the durable store works, and the sealed MCP tools
accept/validate the agents' JSON.

Run directly (``python tests/test_env.py``) or under pytest
(``pytest -q tests/test_env.py``). Each check degrades to a report rather than
crashing the run, so a partial toolchain still yields a readable summary.

    python tests/test_env.py        # exit 0 iff all checks pass
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# Point the durable store at a temp dir BEFORE importing red (constants reads
# RED_DB_ROOT at import time), so the test never touches a real DB.
_TMP_DB = tempfile.mkdtemp(prefix="red-test-db-")
os.environ["RED_DB_ROOT"] = _TMP_DB

from red import constants, db_node, nodes, state_def            # noqa: E402
from red.constants import (                                     # noqa: E402
    COVERAGE_TARGET,
    EXAMPLE_CORE,
    EXAMPLE_PROJECT,
    EXAMPLE_WORKLOAD,
    EXPECTED_TOOLS,
)
from red.tools import ProfileTool, ReportTool, SourceTool, SpecTool  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def _check(name: str, fn) -> None:
    try:
        fn()
        _results.append((name, True, "ok"))
    except Exception as exc:  # noqa: BLE001 — report and continue
        _results.append((name, False, f"{type(exc).__name__}: {exc}"))


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _report() -> state_def.HotLoopReport:
    return state_def.HotLoopReport(
        workload=EXAMPLE_WORKLOAD,
        profile_method="static-asm",
        loops=[
            state_def.HotLoop(loop_id="uECC_vli_mult::inner", source_file="uECC.c",
                              function="uECC_vli_mult", body="for (...) {...}", ir="",
                              cycle_share=0.6, rank=1),
            state_def.HotLoop(loop_id="vli_mmod_fast_secp256r1::inner",
                              source_file="curve-specific.inc",
                              function="vli_mmod_fast_secp256r1", body="for (...) {...}",
                              ir="", cycle_share=0.3, rank=2),
        ],
        coverage=0.9,
        profile_runs=[1000, 1000],
        passes_gate1=True,
    )


def _spec() -> state_def.ISASpec:
    return state_def.ISASpec(
        name="RED-demo",
        version="0.1.0",
        description="demo extension for the environment test",
        instructions=[
            state_def.InstructionSpec(
                name="secp256r1.modadd", mnemonic="modadd",
                encoding="0000001", opcode="custom-0", funct3="000", funct7="0000001",
                operands=["rd", "rs1", "rs2"],
                semantics="rd = (rs1 + rs2) mod p256",
                pseudocode="t = rs1 + rs2; rd = (t >= p) ? t - p : t;"),
            state_def.InstructionSpec(
                name="secp256r1.modmul", mnemonic="modmul",
                encoding="0000010", opcode="custom-0", funct3="000", funct7="0000010",
                operands=["rd", "rs1", "rs2"],
                semantics="rd = (rs1 * rs2) mod p256",
                pseudocode="rd = mulmod(rs1, rs2, p);"),
        ],
    )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def test_imports() -> None:
    import red
    import red.loop
    _require(red.__name__ == "red", "package name")
    # The prompts are loaded by path (LLM_PROMPTS_DIR), not imported as a module.
    for f in ("mine.md", "system.md", "debug.md"):
        _require(os.path.isfile(os.path.join(constants.LLM_PROMPTS_DIR, f)),
                 f"prompt file {f} present")


def test_general_purpose_constants() -> None:
    # No workload/core/hot-function may be hard-coded: the example pair is a
    # default, and source discovery is a function over an input root.
    _require(callable(constants.find_sources), "find_sources must exist")
    srcs = [rel for rel, _ in constants.find_sources(EXAMPLE_PROJECT, exts=(".c",))]
    _require("uECC.c" in " ".join(srcs) or any(s.endswith("uECC.c") for s in srcs),
             "uECC.c discoverable via find_sources (not a hard-coded list)")
    _require(constants.LLM_BACKEND == "gemini", "LLM backend must be Gemini")
    _require(constants.GCP_PROJECT, "GCP project id must be set")


def test_toolchain() -> None:
    tc = nodes._toolchain()
    missing = {g: [t for t, ok in tools.items() if not ok] for g, tools in tc.items()}
    # A1 (the profiling half) needs a compiler and at least one profiler; these
    # are the hard requirement for the research contribution on this host.
    _require(shutil.which("cc") is not None, "cc compiler required for A1")
    have_profile = shutil.which("perf") or shutil.which("valgrind")
    _require(have_profile is not None, "perf or valgrind required for A1")
    # Everything else degrades gracefully; just record it in the summary.
    _results.append(("toolchain", True,
                     "missing (deferred): " +
                     ", ".join(f"{g}:{','.join(ts)}" for g, ts in missing.items() if ts)
                     or "none"))


def test_scripts() -> None:
    """The cluster lifecycle scripts exist and parse: scripts/red_env.sh is the
    entry point (setup/up/down/status) and scripts/gcp_util.py its GCP helper."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sh = os.path.join(root, "scripts", "red_env.sh")
    util = os.path.join(root, "scripts", "gcp_util.py")
    _require(os.path.isfile(sh) and os.access(sh, os.X_OK),
             "scripts/red_env.sh present + executable")
    _require(subprocess.run(["bash", "-n", sh]).returncode == 0,
             "red_env.sh passes bash -n")
    _require(subprocess.run([sys.executable, "-m", "py_compile", util]).returncode == 0,
             "gcp_util.py compiles")
    # The llm node must carry a service account so Vertex ADC needs no login.
    y = open(os.path.join(root, "cluster.yaml")).read()
    _require("service_accounts:" in y and "cloud-platform" in y,
             "cluster.yaml attaches a service account to the llm node")


def test_state_roundtrip() -> None:
    rep = _report()
    spec = _spec()
    state_def.validate_hot_loop_report(rep)
    state_def.validate_isa_spec(spec)

    rep2 = state_def.from_json(state_def.to_json(rep), state_def.HotLoopReport)
    _require(isinstance(rep2.loops[0], state_def.HotLoop),
             "HotLoopReport.loops must rebuild to HotLoop objects")
    state_def.validate_hot_loop_report(rep2)
    _require(abs(rep2.coverage - 0.9) < 1e-9, "coverage survives round-trip")

    spec2 = state_def.from_json(state_def.to_json(spec), state_def.ISASpec)
    _require(isinstance(spec2.instructions[0], state_def.InstructionSpec),
             "ISASpec.instructions must rebuild to InstructionSpec objects")
    state_def.validate_isa_spec(spec2)

    g = state_def.from_json(state_def.to_json(
        state_def.GateResult(gate="gate1", passed=True, detail="ok")),
        state_def.GateResult)
    _require(g.passed and g.gate == "gate1", "GateResult round-trip")


def test_gates() -> None:
    g_pass = nodes.gate1(100_000, 102_000)
    _require(g_pass.passed, f"gate1 2% drift must pass (got: {g_pass.detail})")
    g_fail = nodes.gate1(100_000, 120_000)
    _require(not g_fail.passed, "gate1 20% drift must fail")

    rep = _report()
    spec = _spec()
    l1 = nodes.link1(state_def.to_json(rep), state_def.to_json(spec))
    _require(l1.passed, f"link1 coverage {COVERAGE_TARGET:.0%} must pass (got: {l1.detail})")

    g2 = nodes.gate2(state_def.to_json(spec))
    _require(isinstance(g2, state_def.GateResult), "gate2 returns a GateResult")
    _require(not g2.passed, "gate2 must not pass without a patched Spike")
    l2 = nodes.link2(state_def.to_json(g2))
    _require(l2.passed == g2.passed, "link2 mirrors gate2")


def test_nodes_a3_a5() -> None:
    spec_json = state_def.to_json(_spec())
    a3 = nodes.A3_eval_rtl(EXAMPLE_CORE, spec_json)
    _require(isinstance(a3, state_def.EvalResult), "A3 returns an EvalResult")
    a4 = nodes.A4_eval_compiler(spec_json)
    _require(isinstance(a4, state_def.EvalResult), "A4 returns an EvalResult")
    a5 = nodes.A5_eval_e2e(spec_json, EXAMPLE_CORE)
    _require(isinstance(a5, state_def.SpeedupResult), "A5 returns a SpeedupResult")
    _results.append(("A3/A5 details", True,
                     f"A3={a3.detail!r} | A4={a4.detail!r} | A5={a5.detail!r}"))


def test_verify_chain() -> None:
    rep = _report()
    spec = _spec()
    g2 = nodes.gate2(state_def.to_json(spec))
    a3 = nodes.A3_eval_rtl(EXAMPLE_CORE, state_def.to_json(spec))
    a5 = nodes.A5_eval_e2e(state_def.to_json(spec), EXAMPLE_CORE)
    chain = nodes.verify_chain(state_def.to_json(rep), state_def.to_json(spec),
                               state_def.to_json(g2), state_def.to_json(a3),
                               state_def.to_json(a5))
    _require(len(chain) == 4, "verify_chain assembles exactly 4 links")
    _require([g.gate for g in chain] == ["link1", "link2", "link3", "link4"],
             "chain link names in order")


def test_a1_profile_workload() -> None:
    # Exercises the real harness build + profile fallback on the bundled example
    # (the micro-ecc defines are passed as INPUT, not hard-coded).
    cflags = ("-DuECC_VLI_N_BYTES=32 -DuECC_CURVE=uECC_secp256r1 "
              "-DuECC_WORD_SIZE=8")
    prof = nodes.A1_profile_workload(EXAMPLE_PROJECT, EXAMPLE_WORKLOAD,
                                     os.path.join(_TMP_DB, "profile"), cflags)
    _require(isinstance(prof, dict), "A1_profile_workload returns a dict")
    for k in ("project", "workload", "profile_method", "cycle_counts",
              "available", "command", "harness_built", "tools"):
        _require(k in prof, f"A1 profile missing key {k!r}")
    _results.append(("A1 profile", True,
                     f"method={prof['profile_method']} "
                     f"functions={len(prof['cycle_counts'])} "
                     f"harness_built={prof['harness_built']}"))


def test_db_store() -> None:
    n, path = db_node.claim_run(EXAMPLE_WORKLOAD)
    _require(os.path.isdir(path), "claim_run creates a sweep dir")
    db_node.put(path, "profile.json", '{"a": 1}', workload=EXAMPLE_WORKLOAD)
    db_node.write_text(path, "summary.md", "# hi", workload=EXAMPLE_WORKLOAD)
    _require(os.path.isfile(os.path.join(path, "profile.json")), "put writes the file")
    _require(os.path.isfile(os.path.join(path, "summary.md")), "write_text writes the file")


def test_sealed_tools() -> None:
    import ray
    ray.init(ignore_reinit_error=True)

    report_path = os.path.join(_TMP_DB, "HotLoopReport.json")
    spec_path = os.path.join(_TMP_DB, "ISASpec.json")
    profile_path = os.path.join(_TMP_DB, "profile.json")
    tag = "test"
    tools = [
        SourceTool(name=f"red_src_{tag}", source_roots=[EXAMPLE_PROJECT, EXAMPLE_CORE]),
        ProfileTool(name=f"red_prof_{tag}", profile_path=profile_path),
        ReportTool(name=f"red_report_{tag}", report_path=report_path),
        SpecTool(name=f"red_spec_{tag}", spec_path=spec_path),
    ]
    try:
        src = next(t for t in tools if isinstance(t, SourceTool))
        listing = src.list_sources()
        _require("uECC.c" in listing, "list_sources finds the project sources")

        rep = next(t for t in tools if isinstance(t, ReportTool))
        accepted = rep.write_report(state_def.to_json(_report()))
        _require("Accepted" in accepted, f"write_report accepts valid JSON: {accepted!r}")
        rep2 = state_def.from_json(rep.read_report(), state_def.HotLoopReport)
        _require(isinstance(rep2.loops[0], state_def.HotLoop),
                 "read_report rebuilds nested loops")
        rejected = rep.write_report('{"workload": "x"}')  # missing loops -> invalid
        _require("Rejected" in rejected, "write_report rejects malformed JSON")

        spec = next(t for t in tools if isinstance(t, SpecTool))
        accepted = spec.write_spec(state_def.to_json(_spec()))
        _require("Accepted" in accepted, f"write_spec accepts valid JSON: {accepted!r}")
    finally:
        for t in tools:
            t.stop()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_TESTS = [fn for name, fn in sorted(globals().items())
          if name.startswith("test_") and callable(fn)]


def main() -> int:
    for fn in _TESTS:
        _check(fn.__name__, fn)

    width = max(len(n) for n, _, _ in _results)
    print("\n" + "=" * (width + 28))
    print("RED environment verification")
    print("=" * (width + 28))
    n_ok = 0
    for name, ok, detail in _results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name:<{width}}  {detail}")
        n_ok += ok
    print("-" * (width + 28))
    print(f"  {n_ok}/{len(_results)} checks passed")
    if n_ok != len(_results):
        print(f"  (temp DB left at {_TMP_DB} for inspection)")
    else:
        shutil.rmtree(_TMP_DB, ignore_errors=True)
    print("=" * (width + 28))
    return 0 if n_ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
