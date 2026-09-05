"""Environment verification for RED.

Confirms the repository is set up corresponding to the design's requirements:
imports resolve, the toolchain matches each node's expectations, typed artifacts
round-trip through JSON (nested dataclasses included), the mechanical edges
(Gate 1, link 1, Gate 2, link 2) run — and reject what they are supposed to
reject — the durable store works, and the sealed MCP tools accept/validate the
agents' JSON.

Run directly (``python tests/test_env.py``) or under pytest
(``pytest -q tests/test_env.py``). Each check degrades to a report rather than
crashing the run, so a partial toolchain still yields a readable summary.

    python tests/test_env.py        # exit 0 iff all checks pass
"""

from __future__ import annotations

import json
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

from red import constants, db_node, iss, nodes, state_def       # noqa: E402
from red.constants import (                                     # noqa: E402
    COVERAGE_TARGET,
    EXAMPLE_CFLAGS,
    EXAMPLE_CORE,
    EXAMPLE_HARNESS,
    EXAMPLE_PROJECT,
    EXAMPLE_WORKLOAD,
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


_ADD_MODEL = """
void addcarry_model(const uint32_t in[8], uint32_t out[8]) {
    uint64_t carry = 0;
    for (int i = 0; i < 4; i++) {
        uint64_t s = (uint64_t)in[i] + (uint64_t)in[4 + i] + carry;
        out[i] = (uint32_t)s;
        carry = s >> 32;
    }
    out[4] = (uint32_t)carry;
    for (int i = 5; i < 8; i++) out[i] = 0;
}
"""

_XOR_MODEL = """
void xorfold_model(const uint32_t in[4], uint32_t out[4]) {
    for (int i = 0; i < 4; i++) out[i] = in[i] ^ in[(i + 1) % 4];
}
"""

# Writes one element past the end of out[] — link 1 must reject this.
_OOB_MODEL = """
void oob_model(const uint32_t in[4], uint32_t out[4]) {
    for (int i = 0; i < 5; i++) out[i] = in[i % 4];
}
"""


def _spec() -> state_def.ISASpec:
    return state_def.ISASpec(
        name="RED-demo",
        version="0.1.0",
        description="demo extension for the environment test",
        instructions=[
            state_def.InstructionSpec(
                name="secp256r1.addcarry", mnemonic="addcarry",
                encoding="0000001", opcode="custom-0", funct3="000", funct7="0000001",
                operands=["rd", "rs1", "rs2"], words=8,
                semantics="rd = rs1 + rs2 (128-bit; in[0..3]=rs1, in[4..7]=rs2)",
                pseudocode="t = rs1 + rs2; rd = t;",
                c_model=_ADD_MODEL),
            state_def.InstructionSpec(
                name="secp256r1.xorfold", mnemonic="xorfold",
                encoding="0000010", opcode="custom-0", funct3="000", funct7="0000010",
                operands=["rd", "rs1"], words=4,
                semantics="rd[i] = rs1[i] ^ rs1[(i+1) mod 4]",
                pseudocode="for i in 0..3: rd[i] = rs1[i] ^ rs1[(i+1) % 4]",
                c_model=_XOR_MODEL),
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
    for f in ("mine.md", "system.md", "iss.md", "debug.md"):
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


def test_gate1() -> None:
    g_pass = nodes.gate1(100_000, 102_000)
    _require(g_pass.passed, f"gate1 2% drift must pass (got: {g_pass.detail})")
    g_fail = nodes.gate1(100_000, 120_000)
    _require(not g_fail.passed, "gate1 20% drift must fail")
    _require(not nodes.gate1(0, 0).passed, "gate1 must fail without cycle counts")


def test_link1_accepts_sound_models() -> None:
    """Link 1 builds every C model and stress-runs it under ASan+UBSan."""
    l1 = nodes.link1(state_def.to_json(_report()), state_def.to_json(_spec()),
                     work_dir=os.path.join(_TMP_DB, "link1-ok"))
    _require(l1.passed, f"link1 must pass on sound models: {l1.detail} "
                        f"{l1.counterexample or ''}")
    _results.append(("link1 (sound models)", True, l1.detail))


def test_link1_rejects_broken_model() -> None:
    """The gate has teeth: an out-of-bounds write must be caught, and a spec
    whose coverage is below target must fail even with a clean model."""
    spec = _spec()
    spec.instructions = [state_def.InstructionSpec(
        name="demo.oob", mnemonic="oob", encoding="0000011", opcode="custom-0",
        funct3="000", funct7="0000011", operands=["rd", "rs1"], words=4,
        semantics="deliberately writes out[4] on a 4-word model",
        pseudocode="rd[i] = rs1[i mod 4] for i in 0..4",
        c_model=_OOB_MODEL)]
    l1 = nodes.link1(state_def.to_json(_report()), state_def.to_json(spec),
                     work_dir=os.path.join(_TMP_DB, "link1-oob"))
    _require(not l1.passed, "link1 must reject an out-of-bounds C model")
    _require(l1.counterexample, "a link1 failure must carry a counterexample")

    thin = _report()
    thin.coverage = 0.30
    l1b = nodes.link1(state_def.to_json(thin), state_def.to_json(_spec()),
                      work_dir=os.path.join(_TMP_DB, "link1-thin"))
    _require(not l1b.passed,
             f"link1 must fail below {COVERAGE_TARGET:.0%} coverage")
    _results.append(("link1 (rejects bad)", True,
                     f"OOB caught: {l1.counterexample.splitlines()[0][:90]}"))


def test_gate2_encoding() -> None:
    """The custom-opcode encoding Gate 2 decodes with is derived, not guessed."""
    ins = _spec().instructions[0]
    ins.opcode, ins.funct3, ins.funct7 = "custom-0", "000", "0000001"
    match, mask = iss.match_mask(ins)
    # custom-0 = 0b0001011; funct7=1 sits in bits 31:25.
    _require(match == (1 << 25) | 0x0B, f"match {match:#x} wrong")
    _require(mask == 0xFE00707F, f"mask {mask:#x} must pin funct7+funct3+opcode")
    ins.opcode = "custom-3"
    _require(iss.match_mask(ins)[0] & 0x7F == 0x7B, "custom-3 opcode bits")
    for bad, why in (("custom-9", "unknown opcode"), ("", "empty opcode")):
        ins.opcode = bad
        try:
            iss.match_mask(ins)
        except ValueError:
            continue
        raise AssertionError(f"match_mask must reject {why}")


def test_gate2_rejects_before_running() -> None:
    """Gate 2's cheap structural rejections, which need no Spike at all: an
    encoding collision, and an instruction with no ISS model to compare."""
    spec = _spec()
    spec.passes_gate2 = True          # an agent trying to self-certify
    for i in spec.instructions:       # same slot for both -> collision
        i.opcode, i.funct3, i.funct7 = "custom-0", "000", "0000001"
        i.spike_model = f"void {iss.iss_function(i.mnemonic)}(const uint32_t*, uint32_t*) {{}}"
    ok, detail, counter = iss.run_gate2(spec, os.path.join(_TMP_DB, "g2-collide"), 16)
    ready, _ = iss.toolchain_status()
    if ready:
        _require(not ok and "collision" in detail,
                 f"an encoding collision must fail Gate 2, got: {detail}")
        _require(counter and "claimed by" in counter, "collision names the culprits")

    spec = _spec()                    # distinct slots, but no spike_model
    ok, detail, _ = iss.run_gate2(spec, os.path.join(_TMP_DB, "g2-nomodel"), 16)
    if ready:
        _require(not ok and "spike_model" in detail,
                 f"a missing spike_model must fail Gate 2, got: {detail}")

    g2 = nodes.gate2(state_def.to_json(_spec()), os.path.join(_TMP_DB, "g2-node"))
    _require(isinstance(g2, state_def.GateResult), "gate2 returns a GateResult")
    _require(not g2.passed, "gate2 must never pass a spec with no ISS models")
    l2 = nodes.link2(state_def.to_json(g2))
    _require(l2.passed == g2.passed, "link2 mirrors gate2")
    _results.append(("gate2 toolchain", True,
                     iss.toolchain_status()[1] if not ready else "spike ready"))


def test_spec_validation_requires_c_model() -> None:
    spec = _spec()
    spec.instructions[0].c_model = ""
    try:
        state_def.validate_isa_spec(spec)
    except ValueError:
        pass
    else:
        raise AssertionError("validate_isa_spec must reject a missing c_model")

    spec = _spec()
    spec.instructions[0].c_model = "void wrong_name_model(void) {}"
    try:
        state_def.validate_isa_spec(spec)
    except ValueError:
        pass
    else:
        raise AssertionError("validate_isa_spec must reject a mis-named c_model")

    _require(state_def.c_identifier("secp256r1.modadd") == "secp256r1_modadd",
             "c_identifier maps mnemonics to C identifiers")


def test_spec_validation_encoding_fields() -> None:
    """The encoding fields are what A3/A4 would consume, so they have to be
    well-formed strings in the custom space, and collision-free."""
    def rejects(mutate, why: str) -> None:
        spec = _spec()
        mutate(spec)
        try:
            state_def.validate_isa_spec(spec)
        except ValueError:
            return
        raise AssertionError(f"validate_isa_spec must reject {why}")

    def set_opcode(spec, value):
        spec.instructions[0].opcode = value

    rejects(lambda sp: set_opcode(sp, ""), "an empty opcode")
    rejects(lambda sp: set_opcode(sp, "custom-9"), "an opcode outside custom-0..3")
    rejects(lambda sp: set_opcode(sp, "0001011"), "a raw opcode field")
    # A nested encoding object is the shape agents most often produce.
    rejects(lambda sp: setattr(sp.instructions[0], "encoding",
                               {"opcode": "custom-0", "funct3": "000"}),
            "a non-string encoding")
    # Two instructions on the same (opcode, funct3, funct7) cannot both exist.
    def collide(spec):
        a, b = spec.instructions[0], spec.instructions[1]
        b.opcode, b.funct3, b.funct7 = a.opcode, a.funct3, a.funct7
    rejects(collide, "an encoding collision")

    _require(state_def.normalize_opcode("CUSTOM_1") == "custom-1",
             "normalize_opcode canonicalizes spellings")
    _require(state_def.normalize_opcode("custom0") == "custom-0",
             "normalize_opcode accepts custom0")
    _require(state_def.normalize_opcode("nope") == "",
             "normalize_opcode rejects non-custom opcodes")


def test_spec_tool_normalizes_nested_encoding() -> None:
    """write_spec lifts a nested encoding into the schema instead of bouncing a
    draft that is right in substance but differently shaped."""
    import ray
    ray.init(ignore_reinit_error=True)
    spec = _spec()
    ins = spec.instructions[0]
    payload = json.loads(state_def.to_json(spec))
    payload["instructions"][0]["encoding"] = {
        "opcode": "custom-0", "funct3": "110", "funct7": "0000011"}
    payload["instructions"][0]["opcode"] = ""
    payload["instructions"][0]["funct3"] = ""
    payload["instructions"][0]["funct7"] = ""

    path = os.path.join(_TMP_DB, "ISASpec-nested.json")
    tool = SpecTool(name="red_spec_nested", spec_path=path)
    try:
        out = tool.write_spec(json.dumps(payload))
        _require("Accepted" in out, f"nested encoding must be accepted: {out}")
        back = state_def.from_json(tool.read_spec(), state_def.ISASpec)
        got = back.instructions[0]
        _require(got.opcode == "custom-0" and got.funct3 == "110"
                 and got.funct7 == "0000011" and isinstance(got.encoding, str),
                 f"nested encoding must be lifted into the schema (got {got})")
    finally:
        tool.stop()
    _require(ins.mnemonic, "fixture intact")


def test_a1_profile_workload() -> None:
    # Exercises the real harness build + dynamic profile on the bundled example
    # (the micro-ecc defines are passed as INPUT, not hard-coded).
    prof = nodes.A1_profile_workload(EXAMPLE_PROJECT, EXAMPLE_WORKLOAD,
                                     os.path.join(_TMP_DB, "profile"),
                                     EXAMPLE_CFLAGS, EXAMPLE_HARNESS)
    _require(isinstance(prof, dict), "A1_profile_workload returns a dict")
    for k in ("project", "workload", "profile_method", "cycle_counts",
              "perf_cycles", "available", "command", "harness_main",
              "harness_built", "tools"):
        _require(k in prof, f"A1 profile missing key {k!r}")
    _require(prof["harness_built"],
             f"the example harness must build: {prof.get('build_log', '')[-300:]}")
    _require(prof["harness_main"] == EXAMPLE_HARNESS,
             f"the requested harness must be the one profiled "
             f"(got {prof['harness_main']!r})")
    # A static fallback would silently weaken A1 — the design profiles
    # dynamically, so say so loudly if callgrind did not run.
    _require(prof["profile_method"] == "callgrind",
             f"A1 must profile dynamically, got {prof['profile_method']!r} "
             "(is valgrind/callgrind_annotate installed?)")
    hot = max(prof["cycle_counts"].items(), key=lambda kv: kv[1])
    _results.append(("A1 profile", True,
                     f"method={prof['profile_method']} "
                     f"functions={len(prof['cycle_counts'])} "
                     f"hottest={hot[0]} ({hot[1]:,} Ir)"))


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
