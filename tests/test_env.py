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
import uuid
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
from red.tools import DesignerTool, IssTool  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def _designer_tool(name: str, **over):
    """A DesignerTool with every sealed path defaulted into the temp DB."""
    paths = dict(
        source_roots=[EXAMPLE_PROJECT, EXAMPLE_CORE],
        profile_path=os.path.join(_TMP_DB, "profile.json"),
        report_path=os.path.join(_TMP_DB, "HotLoopReport.json"),
        spec_path=os.path.join(_TMP_DB, "ISASpec.json"),
        status_path=os.path.join(_TMP_DB, "status.md"),
        knowledge_path=os.path.join(_TMP_DB, "knowledge.md"),
        sentinel_path=os.path.join(_TMP_DB, "finished"),
    )
    paths.update(over)
    return DesignerTool(name=name, **paths)


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
                # Gate 3's inputs: what it costs and what it buys.
                mac_ops=0, replaces="uECC_vli_mult::inner", invocations=1,
                marshal_words=0,
                c_model=_ADD_MODEL),
            state_def.InstructionSpec(
                name="secp256r1.xorfold", mnemonic="xorfold",
                encoding="0000010", opcode="custom-0", funct3="000", funct7="0000010",
                operands=["rd", "rs1"], words=4,
                semantics="rd[i] = rs1[i] ^ rs1[(i+1) mod 4]",
                pseudocode="for i in 0..3: rd[i] = rs1[i] ^ rs1[(i+1) % 4]",
                mac_ops=0, replaces="vli_mmod_fast_secp256r1::inner",
                invocations=1, marshal_words=0,
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
    for f in ("mine.md", "system.md", "iss.md", "debug.md",
              "revise.md", "redesign.md", "core.md"):
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
        mac_ops=0, replaces="uECC_vli_mult::inner", invocations=1,
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


def test_spec_validation_requires_cost_fields() -> None:
    """Gate 3 cannot cost an instruction that does not say what it replaces, so
    the schema requires it rather than silently crediting zero benefit."""
    spec = _spec()
    spec.instructions[0].replaces = ""
    try:
        state_def.validate_isa_spec(spec)
    except ValueError as exc:
        _require("replaces" in str(exc), f"the rejection must name the field: {exc}")
    else:
        raise AssertionError("validate_isa_spec must reject a missing `replaces`")

    for field_, bad in (("mac_ops", -1), ("invocations", 0), ("marshal_words", -3)):
        spec = _spec()
        setattr(spec.instructions[0], field_, bad)
        try:
            state_def.validate_isa_spec(spec)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_isa_spec must reject {field_}={bad}")


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
    tool = _designer_tool("red_spec_nested", spec_path=path)
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
    # One server carries the designer's whole surface (red/tools.py).
    dsn = _designer_tool(f"red_dsn_{tag}", profile_path=profile_path,
                         report_path=report_path, spec_path=spec_path)
    try:
        _require("uECC.c" in dsn.list_sources(),
                 "list_sources finds the project sources")

        accepted = dsn.write_report(state_def.to_json(_report()))
        _require("Accepted" in accepted, f"write_report accepts valid JSON: {accepted!r}")
        rep2 = state_def.from_json(dsn.read_report(), state_def.HotLoopReport)
        _require(isinstance(rep2.loops[0], state_def.HotLoop),
                 "read_report rebuilds nested loops")
        rejected = dsn.write_report('{"workload": "x"}')  # missing loops -> invalid
        _require("Rejected" in rejected, "write_report rejects malformed JSON")

        accepted = dsn.write_spec(state_def.to_json(_spec()))
        _require("Accepted" in accepted, f"write_spec accepts valid JSON: {accepted!r}")
        # The point is one server for the whole designer surface, not a
        # particular number of methods on it.
        for m in dsn.METHODS:
            _require(callable(getattr(dsn, m, None)),
                     f"the designer surface stays on one server: {m} missing")
        _require("read_core" in dsn.METHODS,
                 "the designer must be able to re-read the core profile")
        _require({"graph_prior_designs", "graph_prior_findings",
                  "graph_best_design", "graph_loops", "graph_query"}
                 <= set(dsn.METHODS),
                 "and carries the knowledge-graph queries")
        # With no Neo4j running these must degrade, never raise.
        for m in ("graph_best_design", "graph_loops"):
            _require(isinstance(getattr(dsn, m)(), str),
                     f"{m} must always answer with text")
        _require(isinstance(dsn.graph_prior_designs("uECC_vli_mult"), str),
                 "graph_prior_designs must always answer with text")
    finally:
        dsn.stop()


def test_no_prompt_has_an_unsubstituted_placeholder() -> None:
    """A placeholder the loop forgets to fill reaches the agent verbatim, and it
    silently loses a tool it was told to use — `${READ_SPEC}` did exactly that
    in A2's charter. Every placeholder in every charter must be substituted by
    the call site that loads it."""
    import re as _re
    import red.loop as loop
    filled = {
        "mine.md": dict(LIST_SOURCES="a", READ_SOURCE="b", READ_PROFILE="c",
                        WRITE_REPORT="d"),
        "core.md": dict(LIST_SOURCES="a", READ_SOURCE="b"),
        "system.md": dict(GRAPH_PRIOR_DESIGNS="gp", GRAPH_PRIOR_FINDINGS="gf", GRAPH_PRIOR_SECURITY="gs", GRAPH_BEST_DESIGN="gb", GRAPH_LOOPS="gl", GRAPH_QUERY="gq", READ_REPORT="a", READ_PROFILE="b", READ_STATUS="c",
                          READ_KNOWLEDGE="d", READ_SPEC="e", WRITE_SPEC="f",
                          FINISH="g", READ_CORE="h", CORE_PROFILE="i",
                          CRITIC_MAX_ROUNDS="3"),
        "iss.md": dict(READ_SPEC="a", WRITE_SPEC="b", READ_STATUS="c",
                       READ_KNOWLEDGE="d"),
        "debug.md": dict(READ_REPORT="a", READ_SPEC="b", READ_PROFILE="c",
                         READ_STATUS="d", READ_KNOWLEDGE="e", WRITE_REPORT="f",
                         WRITE_SPEC="g", APPEND_KNOWLEDGE="h", VERDICT="i",
                         COUNTEREXAMPLE="j"),
        "revise.md": dict(READ_SPEC="a", READ_REPORT="b", READ_STATUS="c",
                          WRITE_SPEC="d", APPEND_KNOWLEDGE="e", FINISH="f",
                          FINDINGS="g", READ_CORE="h"),
        "redesign.md": dict(READ_SPEC="a", READ_REPORT="b", READ_STATUS="c",
                            WRITE_SPEC="d", APPEND_KNOWLEDGE="e", FINISH="f",
                            TARGET="2.00", VERDICT="g"),
        "secure.md": dict(READ_SPEC="a", READ_REPORT="b", READ_STATUS="c",
                          READ_CORE="d", WRITE_SPEC="e", APPEND_KNOWLEDGE="f",
                          FINISH="g", FINDINGS="h"),
        "security.md": dict(CORE_PROFILE="a", MECHANICAL="b", DIGEST="c",
                            READ_SPEC="d", READ_CORE="e",
                            READ_SECURITY_REPORT="f", READ_KNOWLEDGE="g",
                            PROBE_VECTOR="h", REPORT_FINDINGS="i",
                            MAX_PROBES="24"),
    }
    for name, subs in filled.items():
        text = loop._load_prompt(name, **subs)
        left = sorted(set(_re.findall(r"\$\{[A-Z_0-9]+\}", text)))
        _require(not left, f"{name} leaves {left} unsubstituted")
    _results.append(("agent charters", True,
                     f"{len(filled)} charters, no unsubstituted placeholders"))


def test_core_profile_reaches_the_designer() -> None:
    """A2 designed memory-operand instructions for a coprocessor that cannot
    address memory. A0 exists to prevent that, so its findings have to survive
    parsing, validation, rendering and the prompt substitution."""
    import red.loop as loop
    from red import review

    reply = ("Here is the analysis.\n```json\n" + json.dumps({
        "name": "picorv32", "isa": "rv32im",
        "microarchitecture": "multi-cycle, ~4 cycles per ALU instruction",
        "coprocessor": "PCPI", "coprocessor_memory_access": False,
        "coprocessor_result": "one 32-bit rd", "memory_interface": "native valid/ready",
        "existing_units": ["32x32 multiplier (ENABLE_FAST_MUL)", "sequential divider"],
        "custom_opcode_space": ["custom-0", "custom-1"],
        "register_file": "32 x 32-bit, 2 read ports",
        "cycles_per_load": 5, "cycles_per_alu_op": 3,
        "coprocessor_issue_overhead": 28, "cycles_per_word_moved": 2,
        "area_note": "the core is ~5,600 iCE40 LUT4",
        "constraints": ["PCPI cannot address memory"],
        "evidence": ["picorv32.v ENABLE_FAST_MUL"]}) + "\n```")

    class _Res:
        result = reply
    profile = loop._parse_core_profile(EXAMPLE_CORE, _Res())
    _require(profile is not None, "a fenced JSON reply must parse")
    _require(profile.coprocessor_memory_access is False,
             "the fact that decides everything must survive parsing")
    state_def.validate_core_profile(profile)

    rendered = review.render_core(profile)
    for must in ("CANNOT address memory", "do not duplicate", "CONSTRAINT"):
        _require(must in rendered, f"the designer must be told: {must!r}")

    # and it must actually land in A2's charter, not just exist
    charter = loop._load_prompt(
        "system.md", CORE_PROFILE=rendered, READ_CORE="rc", READ_REPORT="rr",
        READ_PROFILE="rp", READ_STATUS="rs", READ_KNOWLEDGE="rk",
        READ_SPEC="rsp", WRITE_SPEC="ws", FINISH="fin", CRITIC_MAX_ROUNDS="3",
        GRAPH_PRIOR_DESIGNS="gp", GRAPH_PRIOR_FINDINGS="gf",
        GRAPH_PRIOR_SECURITY="gs", GRAPH_BEST_DESIGN="gb", GRAPH_LOOPS="gl",
        GRAPH_QUERY="gq")
    _require("CANNOT address memory" in charter,
             "A2's charter must carry the core profile")
    _require("${" not in charter, "A2's charter has an unsubstituted placeholder")

    # a garbled or incomplete reply must degrade, never crash the run
    class _Bad:
        result = "I could not read the RTL."
    _require(loop._parse_core_profile(EXAMPLE_CORE, _Bad()) is None,
             "an unusable reply yields no profile rather than a broken one")

    class _Partial:
        result = '{"name": "x"}'          # no isa, no coprocessor
    _require(loop._parse_core_profile(EXAMPLE_CORE, _Partial()) is None,
             "an incomplete profile must fail validation, not be half-used")

    # the cost model must retarget from the profile rather than hard-coding
    from red import cost
    slow = cost.Platform.from_core(profile)
    fast = cost.Platform.from_core(state_def.CoreProfile(
        name="f", isa="rv32i", coprocessor="tight",
        coprocessor_issue_overhead=2, cycles_per_word_moved=1))
    _require(fast.instruction_cycles(16, 64) < slow.instruction_cycles(16, 64),
             "a cheaper coprocessor must make Gate 3's arithmetic cheaper")
    # A0 reads the interface (PCPI handshake = 1 cycle); the RTL measured 28
    # end-to-end because a coprocessor also pays for its own FSM. Taking A0's
    # number alone would make Gate 3 optimistic by an order of magnitude.
    _require(slow.fixed_cycles >= cost.FIXED_CYCLES,
             f"A0's interface latency must not weaken the measured overhead "
             f"(got {slow.fixed_cycles} < {cost.FIXED_CYCLES})")
    _require(slow.cycles_per_beat >= cost.CYCLES_PER_BEAT,
             "A0's per-word latency must not weaken the measured beat cost")
    per_mac_now = cost.estimate(state_def.ISASpec(name="z", instructions=[
        state_def.InstructionSpec(
            name="m", mnemonic="m", encoding="e", semantics="s", pseudocode="p",
            c_model="void m_model(const uint32_t in[1], uint32_t out[1]){}",
            words=5, mac_ops=1, replaces="mult::hot", invocations=64,
            marshal_words=2)]),
        state_def.HotLoopReport(workload="w", profile_method="callgrind",
            coverage=0.906,
            loops=[state_def.HotLoop(loop_id="mult::hot", function="f",
                   source_file="x", body="", ir="", dynamic_cycles=14836096,
                   calls=12358, cycle_share=0.423)]),
        profile)
    _require(not per_mac_now.meets_target(),
             "a per-statement design must still be rejected with A0's profile "
             f"applied (got {per_mac_now.app_speedup:.2f}x)")
    _results.append(("A0 core profile", True,
                     f"{profile.name} {profile.isa}, {profile.coprocessor} "
                     f"(mem access {profile.coprocessor_memory_access}), "
                     f"{len(profile.constraints)} constraint(s)"))


def test_cost_model_separates_good_from_bad_designs() -> None:
    """Gate 3 exists because a verified extension was measured 2% slower than
    the baseline. It has to reject that design and accept a fused one, and its
    Amdahl must not forget the cycles A1 never mined."""
    from red import cost
    rep = state_def.HotLoopReport(
        workload="w", profile_method="callgrind", coverage=0.906,
        loops=[state_def.HotLoop(loop_id="mult::hot", function="uECC_vli_mult",
                                 source_file="uECC.c", body="", ir="",
                                 dynamic_cycles=14836096, calls=12358, cycle_share=0.423),
               state_def.HotLoop(loop_id="mmod::hot", function="vli_mmod",
                                 source_file="c.inc", body="", ir="",
                                 dynamic_cycles=16928125, calls=29207, cycle_share=0.483)])

    def ins(m, words, macs, replaces, invocations, marshal=0):
        return state_def.InstructionSpec(
            name=m, mnemonic=m, encoding="e", semantics="s", pseudocode="p",
            c_model=f"void {m}_model(const uint32_t in[1], uint32_t out[1]){{}}",
            words=words, mac_ops=macs, replaces=replaces,
            invocations=invocations, marshal_words=marshal)

    # the shape RED actually shipped: one MAC per invocation, 64 per call
    per_mac = cost.estimate(state_def.ISASpec(
        name="a", instructions=[ins("mac96", 5, 1, "mult::hot", 64, 2)]), rep)
    _require(not per_mac.meets_target(),
             f"a per-statement instruction must be rejected (got {per_mac.app_speedup:.2f}x)")

    # fusing the whole multiply is a big win, but on 42% of cycles Amdahl still
    # caps it below target — the designer has to cover the second kernel too
    one_kernel = cost.estimate(state_def.ISASpec(
        name="b", instructions=[ins("mul256", 16, 64, "mult::hot", 1)]), rep)
    _require(not one_kernel.meets_target(),
             "covering 42% of cycles cannot reach 2x however fast the instruction is")

    both = cost.estimate(state_def.ISASpec(name="c", instructions=[
        ins("mul256", 16, 64, "mult::hot", 1),
        ins("modred", 16, 0, "mmod::hot", 1)]), rep)
    _require(both.meets_target(),
             f"two fused whole-kernel instructions must pass (got {both.app_speedup:.2f}x)")
    _require(both.app_speedup < 1.0 / (1.0 - rep.coverage),
             "Amdahl must be bounded by the unmined remainder, not ignore it")

    _require(cost.work_per_beat(16, 64) == 2.0 and cost.work_per_beat(5, 1) == 0.1,
             "work-per-word is the number the designer is steered by")
    _results.append(("cost model (Gate 3)", True,
                     f"per-MAC {per_mac.app_speedup:.2f}x rejected, "
                     f"one kernel {one_kernel.app_speedup:.2f}x rejected, "
                     f"fused pair {both.app_speedup:.2f}x accepted"))


def test_gate3_measures_declared_cost() -> None:
    """`mac_ops` is the designer's own claim about the price of its design, so
    it is exactly the field worth misreporting — a review round caught a spec
    declaring 0 for a 512-iteration reduction. Gate 3 must measure it."""
    import tempfile
    from red import cost

    honest = """
void mulk_model(const uint32_t in[16], uint32_t out[16]) {
    for (int i = 0; i < 16; i++) out[i] = 0;
    for (int i = 0; i < 8; i++) { uint64_t c = 0;
        for (int j = 0; j < 8; j++) {
            uint64_t p = (uint64_t)in[i]*in[8+j] + out[i+j] + c;
            out[i+j] = (uint32_t)p; c = p >> 32; }
        out[i+8] = (uint32_t)c; }
}"""
    liar = """
void liar_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t r[16]; for (int i=0;i<16;i++) r[i]=in[i];
    for (int k = 0; k < 512; k++) { uint32_t carry = 0;
        for (int i = 0; i < 16; i++) { uint32_t nc = r[i] >> 31;
            r[i] = (r[i] << 1) | carry; carry = nc; } }
    for (int i=0;i<8;i++) out[i]=r[i];
    for (int i=8;i<16;i++) out[i]=0;
}"""

    def ins(m, macs, model):
        return state_def.InstructionSpec(
            name=m, mnemonic=m, encoding="e", semantics="s", pseudocode="p",
            c_model=model, words=16, mac_ops=macs, replaces="k::hot",
            invocations=1)

    rep = state_def.HotLoopReport(
        workload="w", profile_method="callgrind", coverage=0.9,
        loops=[state_def.HotLoop(loop_id="k::hot", function="k", source_file="x",
                                 body="", ir="", dynamic_cycles=14836096,
                                 calls=12358, cycle_share=0.9)])

    with tempfile.TemporaryDirectory() as d:
        spec = state_def.ISASpec(name="t", instructions=[
            ins("mulk", 64, honest), ins("liar", 0, liar)])
        measured = cost.measure_ops(spec, d)
        if not measured:
            _results.append(("Gate 3 cost measurement", True,
                             "skipped: cc/valgrind unavailable"))
            return
        _require(measured.get("liar", 0) > 10 * measured.get("mulk", 1),
                 "a bit-serial reduction must measure far heavier than a multiply")

        est = cost.estimate(spec, rep, None, measured)
        by = {c.mnemonic: c for c in est.per_instruction}
        _require(not by["mulk"].note,
                 f"an honest declaration must stand: {by['mulk'].note}")
        _require("declares mac_ops=0" in by["liar"].note,
                 "a false declaration must be named in the verdict")
        _require(by["liar"].cycles > 10 * by["mulk"].cycles,
                 "the liar must be costed on what it measurably does")

        # and the same spec, costed on the declaration alone, would have passed
        naive = cost.estimate(spec, rep, None, {})
        _require(naive.app_speedup > est.app_speedup,
                 "trusting the declaration must be the optimistic case — that is "
                 "why the measurement exists")
    _results.append(("Gate 3 cost measurement", True,
                     f"honest {measured['mulk']:,} Ir accepted; "
                     f"false claim measured {measured['liar']:,} Ir and repriced "
                     f"{by['liar'].cycles:.0f} cyc"))


def test_gate3_refuses_to_overcredit() -> None:
    """An instruction is credited the work it does, not the work it names.
    Matching `f::inner_loop` to the whole function `f` paid an instruction that
    replaces one iteration for all sixty-four of them."""
    import tempfile
    from red import cost

    small = """
void tiny_model(const uint32_t in[16], uint32_t out[16]) {
    for (int i = 0; i < 16; i++) out[i] = 0;
    uint64_t p = (uint64_t)in[0] * in[8];
    out[0] = (uint32_t)p; out[1] = (uint32_t)(p >> 32);
}"""
    rep = state_def.HotLoopReport(
        workload="w", profile_method="callgrind", coverage=0.9,
        loops=[state_def.HotLoop(loop_id="k::hot", function="k", source_file="x",
                                 body="", ir="", dynamic_cycles=14836096,
                                 calls=12358, cycle_share=0.9)])
    spec = state_def.ISASpec(name="t", instructions=[state_def.InstructionSpec(
        name="tiny", mnemonic="tiny", encoding="e", semantics="s", pseudocode="p",
        c_model=small, words=16, mac_ops=1, replaces="k::hot", invocations=1)])

    with tempfile.TemporaryDirectory() as d:
        measured = cost.measure_ops(spec, d)
        if not measured:
            _results.append(("Gate 3 overcredit", True, "skipped: no cc/valgrind"))
            return
        est = cost.estimate(spec, rep, None, measured)
        c = est.per_instruction[0]
        _require("credited on what it computes" in c.note,
                 f"a one-MAC model claiming a 1200-instruction kernel must be "
                 f"caught (note was {c.note!r})")
        _require(not est.meets_target(),
                 f"and must not pass the gate (got {est.app_speedup:.2f}x)")

    # an unmined loop id earns nothing at all
    spec.instructions[0].replaces = "nowhere::hot"
    none = cost.estimate(spec, rep, None, {})
    _require("matches no mined loop" in none.per_instruction[0].note,
             "an unmined loop id must earn no credit")
    _results.append(("Gate 3 overcredit", True,
                     "one-MAC model cannot claim a whole kernel's cycles"))


def test_review_adjudicates_instead_of_redrawing() -> None:
    """Independent re-readings never converge — each round is free to invent a
    new objection. A reviewer must be shown what it said last time."""
    from red import review
    prior = state_def.ReviewReport(reviewers=["benefit", "legality"], findings=[
        state_def.ReviewFinding(reviewer="benefit", severity="blocking",
                                instruction="mul256", finding="claims the wrong loop"),
        state_def.ReviewFinding(reviewer="legality", severity="minor",
                                instruction="", finding="encoding string is vague")])
    mine = review._prior_digest(prior, "benefit")
    _require("claims the wrong loop" in mine, "a reviewer must see its own finding")
    _require("encoding string is vague" not in mine,
             "and only its own — not another reviewer's")
    _require("verbatim" in mine, "it must be told how to restate an unresolved one")
    _require("first review" in review._prior_digest(None, "benefit"),
             "the first round must say so plainly")

    charter = review._charter("benefit", state_def.to_json(_spec()),
                              state_def.to_json(_report()), EXAMPLE_CORE,
                              None, prior)
    _require("claims the wrong loop" in charter and "${" not in charter,
             "the prior round must reach the charter fully substituted")
    _results.append(("review adjudication", True,
                     "reviewers see their own prior findings"))


def test_review_parses_agent_replies() -> None:
    """The reviewers answer in free text; the loop has to get findings out of it
    however the model wrapped them, and must never crash on a bad reply."""
    from red import review
    cases = {
        "fenced JSON":  ('```json\n[{"instruction":"mac96","severity":"blocking",'
                         '"finding":"slower than software","evidence":"e","fix":"f"}]\n```', 1),
        "bare JSON":    ('[{"severity":"major","finding":"x"}]', 1),
        "JSON in prose": ('I found one issue:\n[{"severity":"minor","finding":"y"}]\ndone.', 1),
        "empty list":   ("[]", 0),
        "no JSON":      ("I was unable to analyse this spec.", 0),
        "not a list":   ('{"severity":"major"}', 0),
    }
    for label, (text, want) in cases.items():
        got = review._parse_findings("benefit", text)
        _require(len(got) == want, f"{label}: expected {want} finding(s), got {len(got)}")
    # an unknown severity must not become "blocking" by accident
    odd = review._parse_findings("benefit", '[{"severity":"catastrophic","finding":"z"}]')
    _require(odd[0].severity == "minor", "unknown severities downgrade, never escalate")

    rep = state_def.ReviewReport(reviewers=["benefit"], findings=[
        state_def.ReviewFinding(reviewer="benefit", severity="blocking",
                                instruction="mac96", finding="net slowdown")])
    _require(rep.blocking() == 1 and len(rep.actionable()) == 1, "severity accounting")
    _require("BLOCKING" in review.render(rep), "render surfaces blocking findings")
    back = state_def.from_json(state_def.to_json(rep), state_def.ReviewReport)
    _require(isinstance(back.findings[0], state_def.ReviewFinding),
             "ReviewReport round-trips like every other artifact")


def test_review_charters_are_complete() -> None:
    """Every reviewer needs a charter, and the shared frame must have no
    unsubstituted placeholders left once the loop fills it in."""
    from red import review
    spec_json = state_def.to_json(_spec())
    report_json = state_def.to_json(_report())
    for reviewer in review.REVIEWERS:
        text = review._charter(reviewer, spec_json, report_json, EXAMPLE_CORE)
        _require("${" not in text, f"{reviewer} charter has an unsubstituted placeholder")
        _require(spec_json[:40] in text, f"{reviewer} charter must carry the spec")
        _require("uECC_vli_mult" in text or "loop" in text,
                 f"{reviewer} charter must carry the hot loops")
    _results.append(("review sub-agents", True,
                     f"{len(review.REVIEWERS)} reviewers: {', '.join(review.REVIEWERS)}"))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_callgrind_call_counts_survive_annotate_thresholds() -> None:
    """Gate 3 prices software per *call*, so a missing call count silently
    disables every benefit it could credit.

    ``callgrind_annotate`` prints a callee row only above a cost threshold, so a
    hot function reached from many individually-cold call sites shows no call
    count at all — which is what valgrind 3.19 did to both micro-ecc kernels
    and cost three full cluster runs. The raw callgrind file records every
    ``calls=`` event exactly, so that is what RED parses."""
    import subprocess
    import tempfile
    from red.nodes import _parse_callgrind_raw

    # A hand-built fixture pins the three format rules the parse depends on:
    # name compression (``fn=(2)`` refers back), the inclusive cost line that
    # follows ``calls=`` and must not land on the caller's self cost, and the
    # ``'2`` recursion suffix folding back into its base name.
    fixture = """# callgrind format
version: 1
creator: callgrind-3.19.0
positions: line
events: Ir
summary: 40

ob=(1) /tmp/x
fl=(1) x.c
fn=(1) main
10 5
cfn=(2) hot
calls=7 20
20 60
30 5

fn=(2)
20 30
cfn=(3) hot'2
calls=3 20
20 10
"""
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "callgrind.out")
        with open(f, "w") as fh:
            fh.write(fixture)
        self_ir, calls = _parse_callgrind_raw(f)
    _require(self_ir.get("main") == 10,
             f"the inclusive cost line after calls= must not be charged to the "
             f"caller (main self was {self_ir.get('main')}, expected 10)")
    _require(self_ir.get("hot") == 30,
             f"a back-reference fn=(2) must resolve to its name "
             f"(hot self was {self_ir.get('hot')}, expected 30)")
    _require(sum(self_ir.values()) == 40,
             "summed self cost must reproduce the file's own summary")
    _require(calls.get("hot") == 7,
             f"calls must count entries from outside the function, excluding "
             f"the folded hot->hot'2 recursion edge (got {calls.get('hot')})")

    # And against ground truth: a program that counts its own calls.
    if not (shutil.which("cc") and shutil.which("valgrind")):
        _results.append(("callgrind call counts", True,
                         "fixture ok; no cc/valgrind for the live check"))
        return
    prog = """#include <stdio.h>
volatile long n_leaf = 0, n_mid = 0, n_rec = 0;
__attribute__((noinline)) int leaf(int x) { n_leaf++; int s = 0;
    for (int i = 0; i < 10; i++) s += x * i; return s; }
__attribute__((noinline)) int mid(int x) { n_mid++; return leaf(x) + leaf(x + 1); }
__attribute__((noinline)) int rec(int d) { n_rec++; return d <= 0 ? 0 : rec(d - 1) + 1; }
int main(void) { long s = 0;
    for (int i = 0; i < 1000; i++) s += mid(i);
    for (int i = 0; i < 50; i++) s += rec(20);
    printf("%ld\\n", s); return 0; }
"""
    with tempfile.TemporaryDirectory() as d:
        c, exe = os.path.join(d, "t.c"), os.path.join(d, "t")
        out = os.path.join(d, "callgrind.out")
        with open(c, "w") as fh:
            fh.write(prog)
        if subprocess.run(["cc", "-O2", "-fno-inline", "-g", "-o", exe, c],
                          capture_output=True).returncode != 0:
            _results.append(("callgrind call counts", True, "fixture ok; build failed"))
            return
        subprocess.run(["valgrind", "--tool=callgrind",
                        f"--callgrind-out-file={out}", "--quiet", exe],
                       capture_output=True)
        if not os.path.exists(out):
            _results.append(("callgrind call counts", True, "fixture ok; no callgrind out"))
            return
        self_ir, calls = _parse_callgrind_raw(out)
    _require(calls.get("mid") == 1000,
             f"mid is called 1000 times (parsed {calls.get('mid')})")
    _require(calls.get("leaf") == 2000,
             f"leaf is called 2000 times (parsed {calls.get('leaf')})")
    _require(calls.get("rec") == 50,
             f"rec is entered 50 times from outside, then recurses 1000 times; "
             f"Gate 3 wants the entries (parsed {calls.get('rec')})")
    _results.append(("callgrind call counts", True,
                     "exact vs ground truth: mid=1000 leaf=2000 rec=50 entries"))


def test_report_is_frozen_after_gate1() -> None:
    """A1 owns the measurement. The designer used to be able to rewrite the
    HotLoopReport — and did, inventing call counts to make its instructions look
    profitable — while Gate 3 scored against the in-memory original."""
    import tempfile
    from red import tools

    rep = state_def.HotLoopReport(
        workload="w", profile_method="callgrind", coverage=0.9,
        loops=[state_def.HotLoop(loop_id="k::hot", function="k", source_file="x",
                                 body="b", ir="i", dynamic_cycles=100,
                                 calls=10, cycle_share=0.9)])
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "HotLoopReport.json")

        class _Bare(tools._ReportMixin):
            report_path = path

        t = _Bare()
        _require("Accepted" in t.write_report(state_def.to_json(rep)),
                 "A1 must be able to write the report before Gate 1")
        open(path + ".frozen", "w").close()
        verdict = t.write_report(state_def.to_json(rep))
        _require(verdict.startswith("Rejected"),
                 f"once frozen the report must be read-only (got {verdict!r})")
        _require("frozen after Gate 1" in verdict,
                 "and the rejection must say why, so the agent stops retrying")
    _results.append(("report frozen after gate 1", True,
                     "A2 cannot edit the evidence it is scored against"))


def test_gate3_names_a_profiling_failure_as_such() -> None:
    """A report with no call counts cannot price any design. Reporting that as
    1.00x blames the designer for a profiler bug and burns the whole redesign
    budget — which is exactly how three cluster runs were lost."""
    from red import cost

    rep = state_def.HotLoopReport(
        workload="w", profile_method="callgrind", coverage=0.9,
        loops=[state_def.HotLoop(loop_id="k::hot", function="k", source_file="x",
                                 body="", ir="", dynamic_cycles=14836096,
                                 calls=0, cycle_share=0.9)])
    spec = state_def.ISASpec(name="t", instructions=[state_def.InstructionSpec(
        name="m", mnemonic="m", encoding="e", semantics="s", pseudocode="p",
        c_model="void m(void){}", words=16, mac_ops=64, replaces="k::hot",
        invocations=1)])
    est = cost.estimate(spec, rep, None, {})
    _require(bool(est.measurement_error),
             "a report with no call counts must be flagged as a measurement "
             "failure, not scored as a bad design")
    _require("profiling failure" in est.measurement_error,
             "and must say so in words the operator can act on")

    # With a call count the same spec is scored normally.
    rep.loops[0].calls = 12358
    ok = cost.estimate(spec, rep, None, {})
    _require(not ok.measurement_error,
             "a report that does carry call counts must score normally")
    _results.append(("gate 3 profiling failure", True,
                     "missing call counts escalate instead of blaming the design"))


def test_ir_per_op_is_calibrated_not_assumed() -> None:
    """How many host instructions a 32x32 MAC compiles to is a property of the
    measuring machine, not of the design.

    Gate 3 compares a model's measured instruction count against its declared
    `mac_ops`, so a wrong ratio makes honest declarations look like
    understatements and overprices the instruction. The old fixed 8.0 was
    fitted on one host; the GCP profiling node measures ~14, and there a
    truthful ``mac_ops=64`` was rejected."""
    import tempfile
    from red import cost

    cost._IR_PER_OP_CACHE.pop("v", None)
    try:
        with tempfile.TemporaryDirectory() as d:
            ratio = cost._ir_per_op(d)
            if not (shutil.which("cc") and shutil.which("valgrind")):
                _results.append(("Ir/op calibration", True, "skipped: no cc/valgrind"))
                return
            _require(ratio > 1.0, f"a MAC costs more than one host instruction "
                                  f"(measured {ratio:.2f})")
            _require(ratio == cost._IR_PER_OP_CACHE.get("v"),
                     "the measured ratio must be the one estimate() uses")
            # An honest declaration must survive on whatever host we are on.
            spec = state_def.ISASpec(name="t", instructions=[
                state_def.InstructionSpec(
                    name="c", mnemonic="calib", encoding="e", semantics="s",
                    pseudocode="p", c_model=cost._CALIB_MODEL, words=16,
                    mac_ops=cost._CALIB_MACS, replaces="k::hot", invocations=1)])
            rep = state_def.HotLoopReport(
                workload="w", profile_method="callgrind", coverage=0.9,
                loops=[state_def.HotLoop(loop_id="k::hot", function="k",
                                         source_file="x", body="", ir="",
                                         dynamic_cycles=14836096, calls=12358,
                                         cycle_share=0.9)])
            measured = cost.measure_ops(spec, os.path.join(d, "m"))
            est = cost.estimate(spec, rep, None, measured)
            _require("costed on the measurement" not in est.per_instruction[0].note,
                     f"a truthful mac_ops={cost._CALIB_MACS} must not be re-priced "
                     f"as an understatement (note: {est.per_instruction[0].note!r})")
    finally:
        cost._IR_PER_OP_CACHE.pop("v", None)
    _results.append(("Ir/op calibration", True,
                     f"measured {ratio:.1f} Ir per MAC on this host; honest "
                     f"declaration stands"))


def test_lost_worker_fails_fast_instead_of_hanging() -> None:
    """A ray.get() on a task whose resource has left the cluster waits forever.

    That cost a nine-hour hang: the workers' reverse SSH tunnels dropped, the
    `llm` resource vanished from the scheduler, and the driver blocked on one
    unschedulable turn while the GCP instances billed. A bare timeout would be
    the wrong fix — callgrind over the whole ECDH exchange legitimately runs for
    ~20 minutes — so the wait polls and asks whether the resource still exists.
    """
    import contextlib
    import io

    import ray
    from ray.exceptions import GetTimeoutError
    from red import loop as red_loop

    real_get, real_res = red_loop.get, ray.cluster_resources
    real_poll, real_max = red_loop.STAGE_POLL_SECONDS, red_loop.STAGE_MAX_SECONDS
    calls = {"n": 0}

    def _never(ref, timeout=None):
        calls["n"] += 1
        raise GetTimeoutError("not yet")

    try:
        red_loop.get = _never
        red_loop.STAGE_POLL_SECONDS = 1.0
        red_loop.STAGE_MAX_SECONDS = 2.0

        # resource gone -> immediate, actionable failure
        ray.cluster_resources = lambda: {"head_local": 8}
        try:
            red_loop._await(object(), "A1 profile", "profile")
            _require(False, "a vanished resource must raise, not hang")
        except RuntimeError as e:
            _require("no longer in the cluster" in str(e),
                     f"the error must name the cause (got {str(e)[:80]!r})")
            _require("red_env.sh up" in str(e),
                     "and tell the operator how to recover")
        _require(calls["n"] == 1,
                 f"it must not keep polling a resource that is gone "
                 f"(polled {calls['n']}x)")

        # resource present -> keep waiting, up to the backstop
        calls["n"] = 0
        ray.cluster_resources = lambda: {"profile": 1}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                red_loop._await(object(), "A1 profile", "profile")
            _require(False, "the backstop must eventually fire")
        except RuntimeError as e:
            _require("still unfinished" in str(e),
                     f"a live-but-wedged task hits the cap (got {str(e)[:80]!r})")
        _require(calls["n"] > 1,
                 "a slow stage on a live resource must be waited on, not failed")

        # and None passes straight through (the --local smoke path)
        _require(red_loop._await(None, "x", "profile") is None,
                 "a None ref must resolve to None")
    finally:
        red_loop.get, ray.cluster_resources = real_get, real_res
        red_loop.STAGE_POLL_SECONDS, red_loop.STAGE_MAX_SECONDS = real_poll, real_max
    _results.append(("lost worker fails fast", True,
                     "vanished resource raises at once; slow stage keeps waiting"))


def test_source_tool_resolves_what_agents_actually_ask_for() -> None:
    """Both agents design from the project's own source, so a source tool that
    cannot open it makes them work from memory.

    A ChiaTool is a Ray actor that can land on any node, where `red` may be
    installed nowhere near the sources. Resolving paths against a repo root
    derived from the module's own location made `list_sources` emit
    ``../../../../../../home/naonao/RED/target_project/...`` and made
    `read_source` reject all of it: 28-32 failed reads per run, every run, with
    the designer guessing ``uECC.c``, ``/home/naonao/RED/...``, ``~naonao/...``
    and never once opening the file it was writing a C model for.
    """
    from red import tools

    class _Bare(tools._SourceMixin):
        source_roots = [EXAMPLE_PROJECT, EXAMPLE_CORE]

    t = _Bare()
    listing = t.list_sources()
    _require("micro-ecc/uECC.c" in listing,
             "the listing must name the project source plainly")
    _require("../" not in listing,
             f"and must not contain traversal segments: {listing[:120]!r}")

    # Every form seen in the failed transcripts must resolve.
    for want in ("micro-ecc/uECC.c", "uECC.c", "test/test_ecdh.c",
                 "/home/naonao/RED/target_project/micro-ecc/uECC.c",
                 "../../../../../../home/naonao/RED/target_project/micro-ecc/uECC.c",
                 "~naonao/RED/target_project/micro-ecc/uECC.c",
                 "picorv32/picorv32.v"):
        body = t.read_source(want)
        _require(not body.startswith("Error"),
                 f"read_source({want!r}) must resolve, got {body[:90]!r}")
        _require(len(body) > 200, f"read_source({want!r}) returned too little")

    # Ambiguity is reported with the choices, not silently guessed.
    amb = t.read_source("start.S")
    _require(amb.startswith("Error") and "ambiguous" in amb,
             f"a name matching two files must say so (got {amb[:90]!r})")
    _require("firmware/start.S" in amb, "and must list the candidates")

    # A miss names the near candidates so the next call succeeds.
    miss = t.read_source("uECC_vli.c")
    _require(miss.startswith("Error"), "an unknown file must be refused")
    _require("list_sources" in miss or "Did you mean" in miss,
             f"and must point somewhere useful (got {miss[:90]!r})")

    # Only indexed files are reachable — there is nothing to escape to.
    for evil in ("/etc/passwd", "../../../../etc/passwd", "red/tools.py"):
        _require(t.read_source(evil).startswith("Error"),
                 f"{evil!r} is not a project source and must be refused")
    _results.append(("source tool resolution", True,
                     "every path form the agents tried resolves; only indexed "
                     "files reachable"))


def test_review_hands_on_its_best_spec_not_its_last() -> None:
    """A revision is not guaranteed to be an improvement.

    Two cluster runs walked their blocking findings down to 1 and then regressed
    on the final round (1 -> 4 and 1 -> 3, one of them also breaking a C model
    so link 1 fell to 1/2). Both were reported as the regression, because the
    loop handed on the latest spec rather than the best one — the same mistake
    Gate 3 already avoids by keeping its best design.
    """
    from red.loop import _review_rank

    def rep(blocking: int, total: int):
        finds = [state_def.ReviewFinding(
            reviewer="benefit", severity="blocking" if i < blocking else "minor",
            instruction="m", finding=f"f{i}") for i in range(total)]
        return state_def.ReviewReport(reviewers=["benefit"], findings=finds)

    ok = state_def.GateResult(gate="link1", passed=True, detail="")
    bad = state_def.GateResult(gate="link1", passed=False, detail="")

    _require(_review_rank(rep(1, 5), ok) < _review_rank(rep(4, 8), ok),
             "one blocking finding must rank better than four")
    _require(_review_rank(rep(1, 2), ok) < _review_rank(rep(3, 9), ok),
             "run 3's round 3 must rank better than its round 4")
    _require(_review_rank(rep(1, 5), ok) < _review_rank(rep(1, 5), bad),
             "with equal findings, a passing link 1 must win")
    _require(_review_rank(rep(0, 3), ok) < _review_rank(rep(1, 1), ok),
             "zero blocking findings must beat one, whatever the total")
    _require(_review_rank(rep(2, 4), ok) < _review_rank(rep(2, 7), ok),
             "on a tie, fewer total findings must win")
    _results.append(("review keeps its best spec", True,
                     "ranked by blocking, then link 1, then total findings"))


def test_reviewers_are_told_what_the_gates_already_settled() -> None:
    """Gate 3 prices every instruction on measured work before the reviewers see
    the spec, so "make it faster" is a closed question. And a reviewer that
    cannot see another's blocking finding cannot know it is contradicting one:
    one run had implementability reject a 16-word operand block as unbuildable
    while benefit asked for exactly that block, leaving the designer to overrule
    a reviewer — and the round it spent doing so regressed the spec."""
    from red import cost, review

    passed = cost.SpeedupEstimate(covered_share=0.9, app_speedup=3.29,
                                  detail="vli_mult: 2.00 MAC/word")
    text = review.render_perf(passed)
    _require("3.29x" in text, "the reviewers must see the measured speedup")
    _require("already settled" in text,
             "and be told the speed question is closed")
    _require("no benefit at all" in text or "no* benefit" in text
             or "out of proportion" in text,
             "while still being allowed to raise a worthless instruction")

    failed = cost.SpeedupEstimate(covered_share=0.9, app_speedup=0.98, detail="d")
    _require("already settled" not in review.render_perf(failed),
             "a spec that failed the gate must not be declared settled")
    _require("not run" in review.render_perf(None)
             or "unestablished" in review.render_perf(None),
             "with no verdict, say so rather than implying one")

    prior = state_def.ReviewReport(
        reviewers=["implementability", "benefit"], findings=[
            state_def.ReviewFinding(reviewer="implementability",
                                    severity="blocking", instruction="mmod256",
                                    finding="a 16-word buffer cannot fit 750 LUTs"),
            state_def.ReviewFinding(reviewer="benefit", severity="blocking",
                                    instruction="mac4",
                                    finding="0.25 MAC/word cannot pay"),
            state_def.ReviewFinding(reviewer="benefit", severity="minor",
                                    instruction="", finding="naming nit")])
    seen = review._others_digest(prior, "benefit")
    _require("16-word buffer" in seen,
             "a reviewer must see the other's blocking finding")
    _require("0.25 MAC/word" not in seen,
             "but not its own — that is what the prior digest is for")
    _require("naming nit" not in seen,
             "and only blocking ones, to keep the contradiction visible")
    _require("authority" in seen and "CoreProfile" in seen,
             "it must be told how a contradiction is settled")
    _require("first review" in review._others_digest(None, "benefit"),
             "the first round must say so plainly")
    _results.append(("reviewers see the gates' verdicts", True,
                     "perf gate closes the speed question; contradictions are "
                     "surfaced with an authority to settle them"))


def test_loop_body_comes_from_the_source_not_the_agent() -> None:
    """The mined loop's body is evidence, and everything downstream reads it.

    A1 is asked to quote the loop it mined; when it could not open the file it
    wrote what it believed instead, and one cluster run recorded the body as
    ``/* Not readable due to path escape error */`` with trip_count 0. The
    designer then wrote a C model for the hottest kernel in the program without
    ever having seen it — that run did not converge, while the run whose report
    carried real source did. So the loop fills the body in mechanically."""
    from red.loop import _BODY_PLACEHOLDER, _function_source, _merge_mechanical

    roots = [EXAMPLE_PROJECT, EXAMPLE_CORE]
    for junk in ("/* Not readable due to path escape error */", "/* Not readable */",
                 "", "   ", "n/a", "see source"):
        _require(bool(_BODY_PLACEHOLDER.search(junk)),
                 f"{junk!r} must be recognised as a non-body")
    real = "for (i = 0; i <= k; ++i) { muladd(left[i], right[k - i], &m0, &m1); }"
    _require(not _BODY_PLACEHOLDER.search(real),
             "a real loop body must never be overwritten")

    body = _function_source(roots, "uECC_vli_mult")
    _require("uECC_vli_mult" in body and "{" in body,
             f"the multiply's definition must be found (got {body[:80]!r})")

    # The secp256r1 reduction lives in curve-specific.inc and is 43% of this
    # workload; it was invisible until .inc joined the source extensions.
    red = _function_source(roots, "vli_mmod_fast_secp256r1")
    _require(red, "the reduction must be findable in curve-specific.inc")
    _require(red.count("vli_mmod_fast_secp256r1(") >= 2,
             "all of its word-size variants must be shown, not just the first")
    _require("curve-specific.inc" in red,
             "and each must be labelled with where it came from")
    _require(not _function_source(roots, "no_such_function_anywhere"),
             "an unknown name must yield nothing rather than a wrong body")

    # End to end through the merge.
    rep = state_def.HotLoopReport(
        workload="w", profile_method="callgrind", coverage=0.9,
        loops=[state_def.HotLoop(
            loop_id="uECC_vli_mult::inner", function="uECC_vli_mult",
            source_file="x", body="/* Not readable */", ir="",
            dynamic_cycles=0, calls=0, cycle_share=0.0)])
    merged = _merge_mechanical(
        rep, {"cycle_counts": {"uECC_vli_mult": 100, "__total__": 100},
              "call_counts": {"uECC_vli_mult": 7}}, source_roots=roots)
    got = merged.loops[0]
    _require("muladd" in got.body or "uECC_vli_mult" in got.body,
             f"the merge must backfill the real body (got {got.body[:80]!r})")
    _require(got.calls == 7, "and still take the call count from the profile")
    _results.append(("loop body from source", True,
                     "placeholders backfilled from the project's own files, "
                     ".inc included"))


def test_source_extensions_cover_the_projects_real_arithmetic() -> None:
    """`.inc` is a C source under another name. micro-ecc keeps every
    curve-specific routine — including the reduction that is 43% of this
    workload's cycles — in `curve-specific.inc`, so omitting the extension hid
    the hottest code in the program from every agent."""
    from red.constants import SOURCE_EXTS, find_sources

    for ext in (".c", ".h", ".inc", ".S", ".v"):
        _require(ext in SOURCE_EXTS, f"{ext} must be a source extension")
    names = {os.path.basename(full) for _rel, full in find_sources(EXAMPLE_PROJECT)}
    _require("curve-specific.inc" in names,
             f"curve-specific.inc must be listed (found {sorted(names)})")
    _require("uECC.c" in names, "and so must uECC.c")
    _results.append(("source extensions", True,
                     f"{len(SOURCE_EXTS)} extensions; curve-specific.inc visible"))


def test_knowledge_graph_accumulates_across_runs() -> None:
    """The graph exists so run N+1 can see what run N tried.

    Everything else RED writes is per-run JSON under output/, which the operator
    deletes between batches; nothing connected the instruction being designed to
    earlier attempts at the same kernel. This exercises the whole path against a
    real Neo4j: two runs, the same kernel named differently by each, and the
    queries the agents actually call.
    """
    from red import cost, graph

    if not graph.available():
        _results.append(("knowledge graph", True,
                         "skipped: no Neo4j (start scripts/setup_neo4j.sh)"))
        return

    wl = "TEST-" + uuid.uuid4().hex[:8]          # isolated from the real graph
    try:
        graph.ensure_schema()

        def _spec_for(mnemonic, words, macs, replaces, invocations=1):
            return state_def.ISASpec(name="S", instructions=[
                state_def.InstructionSpec(
                    name=mnemonic, mnemonic=mnemonic, encoding="e",
                    semantics="s", pseudocode="p", c_model="void m(){}",
                    words=words, mac_ops=macs, replaces=replaces,
                    invocations=invocations, marshal_words=0)])

        def _report_with(loop_id):
            return state_def.HotLoopReport(
                workload=wl, profile_method="callgrind", coverage=0.91,
                loops=[state_def.HotLoop(
                    loop_id=loop_id, function="uECC_vli_mult",
                    source_file="uECC.c", body="for (...) {}", ir="",
                    dynamic_cycles=4873166848, calls=3694592,
                    cycle_share=0.47, rank=1, trip_count=8)])

        # --- run A: a per-iteration instruction that cannot pay -------------
        graph.start_run(wl, "runA", "/p/micro-ecc", "/c/picorv32", model="m")
        graph.record_hot_loops(wl, "runA", _report_with("uECC_vli_mult::inner_loop"))
        graph.record_spec(wl, "runA", _spec_for("mac4", 8, 4,
                                                "uECC_vli_mult::inner_loop", 16),
                          stage="final")
        graph.record_perf(wl, "runA", cost.SpeedupEstimate(
            covered_share=0.9, app_speedup=0.98, detail="d",
            per_instruction=[cost.InstructionCost(
                mnemonic="mac4", words=8, mac_ops=4, cycles=76, beats=16,
                marshal_cycles=80, replaced_cycles=70, speedup=0.9)]), 1)
        graph.record_review(wl, "runA", state_def.ReviewReport(
            reviewers=["benefit"], findings=[state_def.ReviewFinding(
                reviewer="benefit", severity="blocking", instruction="mac4",
                finding="0.25 MAC per operand word cannot pay for its traffic",
                fix="fuse the whole multiply")]), 1)
        graph.finish_run(wl, "runA", converged=False, wallclock=3000,
                         blocking=1, speedup=0.98)

        # --- run B: the whole-kernel instruction, and it converges ----------
        # NOTE the different loop_id for the same kernel — this is what keying
        # hot loops by function rather than by the agent's name is for.
        graph.start_run(wl, "runB", "/p/micro-ecc", "/c/picorv32", model="m")
        graph.record_hot_loops(wl, "runB", _report_with("uECC_vli_mult::region"))
        graph.record_spec(wl, "runB", _spec_for("vlimult", 16, 64,
                                                "uECC_vli_mult::region", 1),
                          stage="final")
        graph.record_perf(wl, "runB", cost.SpeedupEstimate(
            covered_share=0.9, app_speedup=3.30, detail="d",
            per_instruction=[cost.InstructionCost(
                mnemonic="vlimult", words=16, mac_ops=64, cycles=156, beats=32,
                marshal_cycles=0, replaced_cycles=11225, speedup=72.0)]), 1)
        graph.record_gate(wl, "runB", state_def.GateResult(
            gate="gate2", passed=True, detail="2/2"))
        graph.finish_run(wl, "runB", converged=True, wallclock=2400,
                         blocking=0, speedup=3.30)

        # --- one kernel, two runs -------------------------------------------
        loops = graph.loop_history(wl)
        _require(len(loops) == 1,
                 f"two runs naming one kernel differently must share a node "
                 f"(got {len(loops)}: {[l.get('function') for l in loops]})")
        _require(loops[0]["runs_mined"] == 2, "both runs must be on it")
        _require(loops[0]["instructions_designed"] == 2,
                 "and both instructions must hang off it")

        designs = graph.prior_designs(wl, "uECC_vli_mult")
        by = {d["mnemonic"]: d for d in designs}
        _require({"mac4", "vlimult"} <= set(by),
                 f"both designs must be visible (got {sorted(by)})")
        _require(abs(by["vlimult"]["work_per_word"] - 2.0) < 1e-6,
                 "work_per_word must be recorded: 64 MACs / 32 words = 2.0")
        _require(abs(by["mac4"]["work_per_word"] - 0.25) < 1e-6,
                 "and 4 MACs / 16 words = 0.25 — the number that explains why "
                 "the first design lost")
        _require(designs[0]["mnemonic"] == "vlimult",
                 "the converged design must be offered first")
        _require(by["mac4"]["converged"] is False
                 and by["vlimult"]["converged"] is True,
                 "each design must carry its run's outcome")

        findings = graph.prior_findings(wl, "uECC_vli_mult")
        _require(any("cannot pay" in f["finding"] for f in findings),
                 "the objection that sank the first design must be retrievable")

        best = graph.best_design(wl)
        _require(best and best[0]["run"] == "runB" and best[0]["converged"],
                 f"the converged run must rank first (got {best[:1]})")

        # --- the agent-facing tool renders it -------------------------------
        from red import tools

        class _T(tools._GraphMixin):
            workload = wl

        text = _T().graph_prior_designs("uECC_vli_mult")
        _require("vlimult" in text and "mac4" in text,
                 "the tool must render both designs")
        _require("|" in text, "as a table an agent can read")
        _require("cannot pay" in _T().graph_prior_findings("uECC_vli_mult"),
                 "and must surface the prior objection")

        # --- reads only ------------------------------------------------------
        for evil in ("MATCH (n:Run) DETACH DELETE n RETURN 1",
                     "CREATE (n:Run {uid:'x'}) RETURN n",
                     "MATCH (r:Run) SET r.converged = true RETURN r",
                     "MERGE (n:X) RETURN n"):
            try:
                graph.query(evil)
                _require(False, f"a write must be refused: {evil!r}")
            except ValueError as e:
                _require("read" in str(e).lower(),
                         f"and say why (got {e})")
        _require("Rejected" in _T().graph_query("MATCH (n) DETACH DELETE n RETURN 1"),
                 "the tool must refuse a write rather than raise")
        rows = graph.query(f"MATCH (r:Run)-[:OF_WORKLOAD]->"
                           f"(:Workload {{name: '{wl}'}}) RETURN r.run_id AS id")
        _require({r["id"] for r in rows} == {"runA", "runB"},
                 "a legitimate read must work")
        try:
            graph.query("MATCH (n) RETURN n")     # no LIMIT supplied
        except Exception as e:                                 # noqa: BLE001
            _require(False, f"a missing LIMIT must be added, not rejected: {e}")
    finally:
        # Never leave test data in the operator's real graph.
        graph._write(
            "MATCH (w:Workload {name: $wl}) "
            "OPTIONAL MATCH (w)<-[:OF_WORKLOAD]-(r:Run) "
            "OPTIONAL MATCH (r)-[*1..2]->(x) "
            "DETACH DELETE w, r, x", wl=wl)
        graph._write("MATCH (l:HotLoop) WHERE l.workload = $wl DETACH DELETE l",
                     wl=wl)
    _results.append(("knowledge graph", True,
                     "two runs, one kernel node; prior designs, findings and "
                     "the converged best all retrievable; writes refused"))


def test_graph_never_breaks_a_run() -> None:
    """A design run must not depend on a database being up.

    Every write is best-effort and every read degrades to a sentence the agent
    can act on, so a stopped Neo4j costs history and nothing else.
    """
    from red import graph, tools

    saved_driver, saved_state = graph._driver, graph._state
    saved_warned = graph._warned
    try:
        graph._driver, graph._state, graph._warned = None, "unavailable", True
        _require(graph.available() is False, "an unreachable graph reports so")
        # Every write path, with the server 'down'.
        graph.start_run("W", "r", "/p", "/c")
        graph.record_core_profile("c", None)
        graph.record_hot_loops("W", "r", None)
        graph.record_spec("W", "r", None)
        graph.record_perf("W", "r", None, 1)
        graph.record_review("W", "r", None, 1)
        graph.record_gate("W", "r", None)
        graph.finish_run("W", "r", converged=True)
        _require(graph.prior_designs("W", "f") == [],
                 "reads return nothing rather than raising")

        class _T(tools._GraphMixin):
            workload = "W"

        for text in (_T().graph_prior_designs("f"), _T().graph_prior_findings("f"),
                     _T().graph_best_design(), _T().graph_loops(),
                     _T().graph_query("MATCH (n) RETURN n")):
            _require(isinstance(text, str) and "not running" in text,
                     f"the agent must be told plainly, got {text!r}")
    finally:
        graph._driver, graph._state = saved_driver, saved_state
        graph._warned = saved_warned
    _results.append(("graph degrades safely", True,
                     "every write is a no-op and every read explains itself "
                     "when Neo4j is down"))


# ---------------------------------------------------------------------------
# Gate 4 — security
# ---------------------------------------------------------------------------

def _sec_ins(mnemonic: str, model: str, **over):
    """One InstructionSpec carrying *model*, with everything else valid."""
    fields = dict(
        name=mnemonic, mnemonic=mnemonic,
        encoding="0000001 rs2 rs1 000 rd 0001011", semantics="s",
        pseudocode="p", c_model=model, words=8, mac_ops=0,
        replaces="uECC_vli_mult", invocations=1, opcode="custom-0",
        funct3="000", funct7="0000001")
    fields.update(over)
    return state_def.InstructionSpec(**fields)


_SEC_CLEAN = """
void clean_model(const uint32_t in[8], uint32_t out[8]) {
    uint64_t carry = 0;
    for (int i = 0; i < 4; i++) {
        uint64_t s = (uint64_t)in[i] + (uint64_t)in[4 + i] + carry;
        out[i] = (uint32_t)s; carry = s >> 32;
    }
    out[4] = (uint32_t)carry;
    for (int i = 5; i < 8; i++) out[i] = 0;
}
"""
# One word past the end of the output block.
_SEC_OOB = """
void oob_model(const uint32_t in[8], uint32_t out[8]) {
    for (int i = 0; i < 8; i++) out[i] = in[i];
    out[8] = 1u;
}
"""
# Writes half its output block and leaves the rest holding the caller's data.
_SEC_RESIDUE = """
void residue_model(const uint32_t in[8], uint32_t out[8]) {
    for (int i = 0; i < 4; i++) out[i] = in[i] ^ in[4 + i];
}
"""
# A conditional subtraction and an early exit -- the classic leak in a modular
# reduction, and the one the designer's charter tells it to avoid.
_SEC_VARTIME = """
void vartime_model(const uint32_t in[8], uint32_t out[8]) {
    uint32_t acc = 0;
    for (int i = 0; i < 8; i++) {
        if (in[i] & 1u) acc += in[i]; else acc ^= 3u;
        out[i] = acc;
    }
    while (acc > 1000u) { acc >>= 1; out[0] = acc; }
}
"""


def test_security_gate_is_decided_by_machines_not_by_opinions() -> None:
    """The point of Gate 4: every defect below is *executed into existence*,
    not reasoned about. A language model asked whether these models are safe
    would be guessing; a sanitizer, a second call against a different poison,
    and an instruction count are not.

    Each case is a defect a verified, fast extension can still have — link 1
    and Gate 2 pass all of them, because they check that a model means what the
    spec says, not what it does to the machine.
    """
    from red import security

    work = tempfile.mkdtemp(prefix="red-test-sec-")
    try:
        clean = state_def.ISASpec(name="s", instructions=[
            _sec_ins("clean", _SEC_CLEAN)])
        rep = security.analyze(clean, None, os.path.join(work, "clean"),
                               vectors=64)
        got = security.verdict(rep)
        _require(got.passed,
                 f"a sound model must pass Gate 4, got: {got.detail} "
                 + "; ".join(f.finding for f in rep.findings))
        _require(rep.checks_run, "a passing verdict must name the checks it ran")

        cases = {
            "memory.bounds": _sec_ins("oob", _SEC_OOB),
            "memory.output_residue": _sec_ins("residue", _SEC_RESIDUE),
            "timing.data_dependent": _sec_ins("vartime", _SEC_VARTIME),
            "model.forbidden_call": _sec_ins(
                "libc", "#include <stdlib.h>\n"
                "void libc_model(const uint32_t in[8], uint32_t out[8]) {\n"
                "  uint32_t *t = (uint32_t *)malloc(32);\n"
                "  for (int i = 0; i < 8; i++) t[i] = in[i];\n"
                "  for (int i = 0; i < 8; i++) out[i] = t[i];\n"
                "  free(t); }"),
            "encoding.opcode": _sec_ins(
                "sys", _SEC_CLEAN.replace("clean_model", "sys_model"),
                encoding="0000001 rs2 rs1 000 rd 1110011"),
        }
        caught = []
        for check, ins in cases.items():
            spec = state_def.ISASpec(name="s", instructions=[ins])
            rep = security.analyze(spec, None, os.path.join(work, check),
                                   vectors=64)
            got = security.verdict(rep)
            names = [f.check for f in rep.findings if f.severity == "blocking"]
            if check == "timing.data_dependent" and \
                    any("could not run" in s_ for s_ in rep.checks_skipped) and \
                    check not in names:
                continue          # valgrind unusable here; honestly skipped
            _require(check in names,
                     f"{check} was not caught; found {names or 'nothing'}")
            _require(not got.passed, f"{check} must fail Gate 4")
            _require(got.counterexample,
                     f"{check} must hand back the evidence it was found with")
            caught.append(check)
        _results.append(("security gate", True,
                         "caught " + ", ".join(caught)
                         + "; a sound model passes"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_security_never_fails_a_design_on_an_agents_word() -> None:
    """An agent cannot block a spec by asserting something, and a probe it
    proposes *can* — that asymmetry is the whole design.

    Without it there are only two failure modes, both bad: a checker a model
    can talk into rejecting a sound extension, or one that ships an unsafe one
    because the model said it looked fine.
    """
    from red import security

    work = tempfile.mkdtemp(prefix="red-test-sec-agent-")
    try:
        # What the agent says, however confidently, is advisory.
        claimed = security.parse_agent_findings("""[
          {"instruction": "clean", "class": "side_channel",
           "severity": "blocking",
           "finding": "I believe this leaks the scalar through timing",
           "evidence": "it looks like it might", "fix": "rewrite it"}]""")
        _require(len(claimed) == 1, "the reply must parse")
        _require(claimed[0].severity == "major",
                 f"an asserted finding must be capped at major, got "
                 f"{claimed[0].severity}")
        _require(claimed[0].confidence == "agent", "and marked as the agent's")

        spec = state_def.ISASpec(name="s",
                                 instructions=[_sec_ins("clean", _SEC_CLEAN)])
        rep = security.merge(
            security.analyze(spec, None, os.path.join(work, "a"), vectors=32),
            claimed)
        _require(security.verdict(rep).passed,
                 "an agent's assertion must not fail the gate")
        _require(rep.blocking() == 0 and len(rep.actionable()) == 1,
                 "but it must still reach the designer")

        # A vector it proposes that actually breaks the model does block.
        ins = _sec_ins("residue", _SEC_RESIDUE)
        text, finding = security.probe(ins, "ff" * 32, os.path.join(work, "p"))
        _require(finding is not None,
                 f"a probe that breaks the model must produce a finding: {text}")
        _require(finding.confidence == "confirmed" and
                 finding.severity == "blocking",
                 "a probe result is evidence, so it blocks")
        rep2 = security.merge(
            security.analyze(state_def.ISASpec(name="s", instructions=[ins]),
                             None, os.path.join(work, "b"), vectors=32),
            [finding])
        _require(not security.verdict(rep2).passed, "and the gate fails")

        # A probe on a sound model reports cleanly and blocks nothing.
        text, finding = security.probe(_sec_ins("clean", _SEC_CLEAN),
                                       "00" * 32, os.path.join(work, "c"))
        _require(finding is None and "Clean" in text,
                 f"a clean probe must not invent a finding: {text[:200]}")
        # And a malformed vector is refused rather than silently truncated.
        text, finding = security.probe(_sec_ins("clean", _SEC_CLEAN), "dead",
                                       os.path.join(work, "d"))
        _require(finding is None and "Rejected" in text,
                 f"a short vector must be refused: {text[:200]}")
        _results.append(("security agent bounds", True,
                         "assertions are advisory; probes that break a model "
                         "are confirmed and block"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_security_check_that_did_not_run_is_not_a_check_that_passed() -> None:
    """The failure mode that makes a security report worse than none.

    A host without valgrind cannot answer the constant-time question, and a
    report that quietly omits it reads exactly like one where the answer was
    "no leak". Both the report and the gate's own detail have to say so — and a
    host that can run nothing at all must fail, not pass by default.
    """
    from red import security

    work = tempfile.mkdtemp(prefix="red-test-sec-skip-")
    saved = shutil.which
    try:
        spec = state_def.ISASpec(name="s",
                                 instructions=[_sec_ins("vartime", _SEC_VARTIME)])
        shutil.which = lambda tool, *a, **k: (None if tool == "valgrind"
                                              else saved(tool, *a, **k))
        rep = security.analyze(spec, None, os.path.join(work, "novg"),
                               vectors=32)
        _require(any("valgrind" in s_ for s_ in rep.checks_skipped),
                 "a missing valgrind must be reported, not ignored")
        _require("timing.data_dependent" not in rep.checks_run,
                 "and the check must not be claimed as run")
        _require("could not run" in security.verdict(rep).detail,
                 "the gate's own one-line detail must carry it")
        _require("NOT run" in security.render(rep) or
                 "could NOT run" in security.render(rep),
                 "and the report the designer reads must say it plainly")

        shutil.which = lambda tool, *a, **k: None
        rep = security.analyze(spec, None, os.path.join(work, "bare"),
                               vectors=32)
        _require(not security.verdict(rep).passed,
                 "a host that can check nothing must not pass the gate")
        _results.append(("security skips are reported", True,
                         "a missing tool is named in the report, in the gate's "
                         "detail, and never passes by default"))
    finally:
        shutil.which = saved
        shutil.rmtree(work, ignore_errors=True)


def test_security_tool_cannot_edit_what_it_reviews() -> None:
    """The security agent's sealed surface.

    It reads the spec and runs the instruction; it cannot write the spec. A
    reviewer that can edit what it reviews is not a reviewer — and this one has
    a stronger reason than the others: its findings are fed straight back to the
    designer, so a tool that let it rewrite the design would let one agent's
    unverified opinion become the artifact.
    """
    from red.tools import SecurityTool

    work = tempfile.mkdtemp(prefix="red-test-sec-tool-")
    try:
        spec_path = os.path.join(work, "ISASpec.json")
        with open(spec_path, "w") as f:
            f.write(state_def.to_json(state_def.ISASpec(
                name="s", instructions=[_sec_ins("clean", _SEC_CLEAN)])))
        findings = os.path.join(work, "agent.json")
        # Built without starting the actor: this is about the method surface,
        # and a Ray actor per assertion is minutes of test time.
        tool = object.__new__(SecurityTool)
        tool.spec_path = spec_path
        tool.core_path = os.path.join(work, "CoreProfile.json")
        tool.security_path = os.path.join(work, "report.md")
        tool.findings_path = findings
        tool.probe_dir = work
        tool.knowledge_path = os.path.join(work, "knowledge.md")
        tool.probes = 0

        _require(not any(m.startswith("write_spec") for m in SecurityTool.METHODS),
                 f"the security agent must not be able to write the spec: "
                 f"{SecurityTool.METHODS}")
        _require("probe_vector" in SecurityTool.METHODS,
                 "but it must be able to run the instruction")
        _require("clean" in tool.read_spec(), "it must be able to read the spec")

        out = tool.probe_vector("nosuch", "00" * 32)
        _require("No instruction" in out and "clean" in out,
                 f"an unknown mnemonic must name the real ones: {out[:200]}")
        out = tool.probe_vector("clean", "00" * 32, why="all zeros")
        _require("Clean" in out, f"a sound vector must come back clean: {out[:200]}")
        _require(not os.path.exists(findings),
                 "a clean probe must not record a finding")

        out = tool.report_findings('[{"instruction": "clean", "class": "x", '
                                   '"severity": "blocking", "finding": "f", '
                                   '"evidence": "e", "fix": "z"}]')
        _require("Recorded 1" in out, out)
        with open(findings) as f:
            saved_f = json.load(f)
        _require(saved_f[0]["severity"] == "major" and
                 saved_f[0]["confidence"] == "agent",
                 f"the tool must record it as advisory: {saved_f[0]}")
        _results.append(("security tool", True,
                         "read + probe, no write_spec; asserted findings land "
                         "as advisory"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_every_tool_a_charter_offers_actually_has_a_name() -> None:
    """`_tool_map` names the tools the charters interpolate. It used to hold its
    own copy of the method list, so a method added to a tool but forgotten here
    substituted as the empty string — the charter then told the agent to call a
    tool with no name, and the agent simply never used it. The map has to be
    derived from each tool's own METHODS, and every method has to survive the
    64-character cap the backend imposes on tool names."""
    import red.loop as loop
    from red.tools import DesignerTool, IssTool, SecurityTool

    class _Stub:
        def __init__(self, cls):
            self.name = "red_dsn_abc123"
            self.METHODS = cls.METHODS

    for cls in (DesignerTool, IssTool, SecurityTool):
        stub = _Stub(cls)
        names = loop._tool_map([stub])
        missing = [m for m in cls.METHODS if not names.get(m)]
        _require(not missing, f"{cls.__name__}: {missing} have no tool name")
        clipped = [m for m in cls.METHODS
                   if len(f"{stub.name}__{stub.name}_{m}") > 64]
        _require(not clipped,
                 f"{cls.__name__}: {clipped} are truncated by the 64-char cap")
    _results.append(("tool names", True,
                     "every method of every sealed tool resolves to a name the "
                     "charters can interpolate"))


def test_no_configuration_is_imported_and_then_ignored() -> None:
    """A knob the driver imports but never passes is a knob that does nothing.

    `LLM_MAX_TOKENS` was exactly that: `red/constants.py` documented it as
    raising the backend's 16,000-token default, `red/loop.py` imported it, and
    the `VertexGeminiLLM(...)` call never passed it. Every agent turn ran at the
    default, so a reply carrying two C models — or the ISS agent's two Spike
    models — was truncated and arrived as `MaxOutputTokensError`, i.e. as a
    *failed turn*. Three runs in one cluster batch died that way, and the symptom
    ("the backend is erroring") pointed nowhere near the cause.

    So: every name the driver imports from `red.constants` must be used
    somewhere in it. This is the general form of that bug, not the instance.
    """
    import ast as _ast

    src = open(os.path.join(constants.REPO_ROOT, "red", "loop.py")).read()
    tree = _ast.parse(src)
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and (node.module or "").endswith(
                "constants"):
            imported |= {a.asname or a.name for a in node.names}
    used = {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name)}
    used |= {n.attr for n in _ast.walk(tree) if isinstance(n, _ast.Attribute)}
    # A name used only in its own import statement is not used.
    dead = sorted(n for n in imported
                  if sum(1 for x in _ast.walk(tree)
                         if isinstance(x, _ast.Name) and x.id == n) == 0)
    _require(not dead,
             f"red/loop.py imports {dead} from red.constants and never uses "
             "them — configuration that silently does nothing")
    _results.append(("no dead configuration", True,
                     f"all {len(imported)} constants the driver imports are "
                     "actually used"))


def test_a_secure_design_is_part_of_converging() -> None:
    """Gate 4 has to *count*. A run that reports success on an extension with a
    confirmed timing leak would be worse than one that never checked, because it
    would say the question had been asked."""
    import inspect
    import red.loop as loop
    from red import review

    src = inspect.getsource(loop.run_red_loop)
    _require("secure = bool(gate4 and gate4.passed)" in src and
             "and secure and" in src,
             "the convergence test must require Gate 4")
    _require(src.index("_scan(state_def.to_json(spec)") > src.index("review_spec"),
             "the shipped spec must be re-checked AFTER the review may have "
             "revised it")

    # ... and the reviewers must be told what it settled, or they re-open it.
    rep = state_def.SecurityReport(
        checks_run=["memory.bounds", "timing.data_dependent"],
        checks_skipped=["timing.secret_branch: memcheck could not run"],
        findings=[state_def.SecurityFinding(
            check="agent.side_channel", severity="major", confidence="agent",
            instruction="clean", finding="worth a second look")],
        vectors=512)
    text = review.render_security(rep)
    for must in ("No mechanically confirmed security defect",
                 "could NOT run", "worth a second look"):
        _require(must in text, f"reviewers must be told: {must!r}")
    _require("not established mechanically" in text,
             "and must be able to tell the agent's word from a measurement")

    result = loop.REDResult(
        workload="w", converged=False, iterations=1,
        gate4=state_def.GateResult(gate="gate4", passed=False, detail="d"),
        security=rep)
    summary = loop._render_summary(result, 1)
    _require("Gate 4 (security): FAIL" in summary, summary[:400])
    _require("NOT CHECKED" in summary,
             "the summary must carry what was not checked, not just what was")
    _results.append(("security in the verdict", True,
                     "Gate 4 gates convergence, is re-run on the shipped spec, "
                     "and reaches the reviewers and the summary"))


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
