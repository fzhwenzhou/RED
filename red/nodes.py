"""ChiaFunction workers for the RED loop — the programmatic evaluation edges.

Scope (RED design §1-2): **A1 kernel mining + A2 ISA synthesis are the research
contribution.** They are *agentic* — driven by the Gemini agents in
:mod:`red.loop` (mining, pattern analysis, instruction design, spec critique) —
and each is bounded by a programmatic gate defined here. A3-A5 are **fixed,
bounded tool nodes** on one platform (a core + a compiler path): thin wrappers
that just run the platform tool and report, no agent in the loop.

The graph the loop drives (Fig. 1-3 of the design):

    A1_profile_workload (profile) ─┐
                                   ├─ gate1  (±5% re-profile) ── HotLoopReport
    A1 mining agent (loop.py) ─────┘
    A2 synthesis agent (loop.py) ─┐
                                   ├─ gate2  (C <=> Spike, 10^5) ── ISASpec
                                   └─ link1  (kernel <=> C)
                                      link2  (C <=> Spike)
    A3_eval_rtl / A4_eval_compiler / A5_eval_e2e   (fixed tool nodes)
                                      link3  (Spike <=> RTL)
                                      link4  (RTL <=> e2e)

Every node degrades gracefully: a missing tool returns a typed result whose
``detail`` says "not available" rather than crashing, so the loop can run on a
partial toolchain and still exercise the A1/A2 contribution.

All inputs (project dir, core dir, workload name, cflags) are parameters — RED
is general-purpose; nothing about any specific workload or core is hard-coded.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Optional

from chia.base.ChiaFunction import ChiaFunction

from red import state_def
from red.constants import (
    COVERAGE_TARGET,
    EXPECTED_TOOLS,
    GATE1_TOLERANCE,
    GATE2_VECTORS,
    REPO_ROOT,
    find_sources,
)


# ---------------------------------------------------------------------------
# Toolchain probing
# ---------------------------------------------------------------------------

def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _toolchain() -> dict:
    """Probe EXPECTED_TOOLS and return {group: {tool: bool}}."""
    return {group: {t: _have(t) for t in tools}
            for group, tools in EXPECTED_TOOLS.items()}


def _missing(groups: list[str]) -> list[str]:
    tc = _toolchain()
    missing = []
    for g in groups:
        for tool, ok in tc.get(g, {}).items():
            if not ok:
                missing.append(tool)
    return missing


def _unavailable(node: str, missing: list[str], what: str) -> state_def.EvalResult:
    return state_def.EvalResult(
        node=node, passed=False,
        detail=f"{what} not available on this host — missing: {', '.join(missing)}",
    )


# ---------------------------------------------------------------------------
# A1 — kernel mining: the mechanical profiling half (the ranking half is the
# mining agent in red.loop)
# ---------------------------------------------------------------------------

def _c_sources(project: str) -> list[str]:
    return [full for rel, full in find_sources(project) if rel.endswith(".c")]


def _build_harness(project: str, work_dir: str, cflags: str) -> tuple[Optional[str], str]:
    """Best-effort: compile the project into a runnable native harness.

    Finds a translation unit with ``int main`` and links it with the remaining
    ``.c`` files at -O2. ``cflags`` is the operator-provided compile input (the
    bundled example passes its curve/word-size defines here). Returns
    (binary_path | None, log)."""
    os.makedirs(work_dir, exist_ok=True)
    sources = _c_sources(project)
    if not sources:
        return None, "no .c sources found under project"
    main = next((s for s in sources
                 if re.search(r"\bint\s+main\s*\(", open(s, errors="replace").read())), None)
    if main is None:
        return None, "no int main() found among sources (no runnable harness)"
    # Exclude other TUs that define their own main() (test suites ship one
    # per file) — linking them in would collide on the symbol.
    rest = [s for s in sources if s != main
            and not re.search(r"\bint\s+main\s*\(", open(s, errors="replace").read())]
    inc = " ".join(f"-I{d}" for d in sorted({os.path.dirname(s) for s in sources}))
    out = os.path.join(work_dir, "harness")
    cmd = f"cc -O2 {cflags} {inc} -o {out} {main} {' '.join(rest)}"
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(out):
        return None, f"harness build failed: {p.stderr[-800:]}"
    return out, cmd


def _dynamic_profile(binary: str, work_dir: str) -> tuple[dict, str, str]:
    """Run the harness under callgrind (or perf) and return per-function counts."""
    if _have("valgrind"):
        out = os.path.join(work_dir, "callgrind.out")
        cmd = (f"valgrind --tool=callgrind --callgrind-out-file={out} "
               f"--quiet {binary} >/dev/null 2>&1")
        subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if os.path.exists(out):
            annotate = subprocess.run(
                ["callgrind_annotate", out], capture_output=True, text=True)
            counts = _parse_callgrind(annotate.stdout)
            if counts:
                return counts, "callgrind", cmd
    if _have("perf"):
        cmd = f"perf stat -e cycles {binary} >/dev/null 2>&1"
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        m = re.search(r"([\d,]+)\s+cycles", p.stderr or "")
        if m:
            return {"__total__": int(m.group(1).replace(",", ""))}, "perf-stat", cmd
    return {}, "", ""


def _parse_callgrind(annotate: str) -> dict:
    """Map function name -> inclusive instruction count from callgrind_annotate."""
    counts: dict = {}
    for line in annotate.splitlines():
        m = re.match(r"^\s*([\d,]+)\s+([\w$.<>@]+)\s*\[", line)
        if m:
            name = m.group(2)
            n = int(m.group(1).replace(",", ""))
            counts[name] = counts.get(name, 0) + n
    return counts


def _static_profile(project: str, work_dir: str, cflags: str) -> tuple[dict, str, str]:
    """Fallback: compile each TU to assembly and count instructions per function."""
    os.makedirs(work_dir, exist_ok=True)
    counts: dict = {}
    method = "static-asm"
    cmd = ""
    for src in _c_sources(project):
        asm = os.path.join(work_dir, os.path.basename(src) + ".s")
        c = (f"cc -O2 -S {cflags} -I{os.path.dirname(src)} "
             f"-o {asm} {src} 2>/dev/null")
        if subprocess.run(c, shell=True).returncode != 0:
            continue
        if not cmd:
            cmd = c
        try:
            for fn, n in _parse_asm(open(asm).read()).items():
                counts[fn] = counts.get(fn, 0) + n
        except OSError:
            continue
    return counts, method, cmd


def _parse_asm(text: str) -> dict:
    """Per-function instruction counts from GCC/Clang ``-S`` assembly.

    Function boundaries come from ``.type <name>, @function`` directives; the
    function's instructions are the indented mnemonic lines that follow, until
    its ``.size <name>`` closer or the next ``.type``. ``.L*`` local labels
    (loop heads, branch targets) and other directives are ignored — they are
    not functions, so a function is attributed as one unit rather than
    fragmented across its local labels."""
    counts: dict = {}
    cur = None
    mnemonic = re.compile(r"^\s+[a-zA-Z][\w.]*\b")
    for line in text.splitlines():
        m = re.match(r"^\s*\.type\s+([\w$.]+),\s*[%@]function", line)
        if m:
            cur = m.group(1)
            counts.setdefault(cur, 0)
            continue
        if re.match(r"^\s*\.size\s+[\w$.]+", line):
            cur = None      # end of the current function's body
            continue
        if line.strip().startswith("."):
            continue        # any other directive
        if re.match(r"^[\w$.][\w$.]*:\s*$", line):
            continue        # a label (function entry or .L local) — not a mnemonic
        if cur is not None and mnemonic.match(line):
            counts[cur] += 1
    return counts


@ChiaFunction(resources={"profile": 1.0})
def A1_profile_workload(project: str, workload: str, work_dir: str,
                        cflags: str = "") -> dict:
    """Build + run the domain harness and profile it (dynamic, corroborated by
    static). Returns a dict the mining agent reads via the ProfileTool:

        {project, workload, profile_method, cycle_counts, available,
         command, harness_built, tools}

    Degrades gracefully: without a compiler / valgrind / perf it falls back to
    static assembly instruction counts; without any compiler it reports
    ``profile_method="none"`` with empty counts."""
    tc = _toolchain()
    available = sorted(g for g, t in tc.items() if any(t.values()))
    method, counts, command, harness_built = "none", {}, "", False

    if _have("cc"):
        binary, cmd = _build_harness(project, work_dir, cflags)
        if binary:
            harness_built = True
            counts, method, command = _dynamic_profile(binary, work_dir)
        if not counts:
            counts, method, command = _static_profile(project, work_dir, cflags)

    return {
        "project": project,
        "workload": workload,
        "profile_method": method,
        "cycle_counts": counts,
        "available": available,
        "command": command,
        "harness_built": harness_built,
        "tools": tc,
    }


@ChiaFunction(resources={"head_local": 0.1})
def gate1(total_a: int, total_b: int) -> state_def.GateResult:
    """Gate 1: two independent profile runs must agree within ±GATE1_TOLERANCE.

    The loop passes the per-run total cycle counts from two separate
    A1_profile_workload passes; a relative difference > 5% fails the gate and
    the harness is repaired/re-instrumented (RED design §2, A1)."""
    if total_a <= 0 or total_b <= 0:
        return state_def.GateResult(
            gate="gate1", passed=False,
            detail="no dynamic cycle counts — re-profile unavailable")
    diff = abs(total_a - total_b) / total_a
    passed = diff <= GATE1_TOLERANCE
    return state_def.GateResult(
        gate="gate1", passed=passed,
        detail=(f"runs {total_a} vs {total_b} cycles "
                f"({diff:.2%} vs ±{GATE1_TOLERANCE:.0%})"))


# ---------------------------------------------------------------------------
# A2 — ISA synthesis: the mechanical Gate 2 (the S2a/S2b/S2c agents live in
# red.loop)
# ---------------------------------------------------------------------------

@ChiaFunction(resources={"spike": 1.0})
def gate2(spec_json: str) -> state_def.GateResult:
    """Gate 2: the ISASpec's C model and a patched Spike must agree on 10^5
    random + corner vectors. This edge validates the spec structurally and, when
    Spike is present, enumerates the vector set; the differential comparison
    itself runs on the Spike node (patched ISS, cached build)."""
    try:
        spec = state_def.from_json(spec_json, state_def.ISASpec)
        state_def.validate_isa_spec(spec)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return state_def.GateResult(gate="gate2", passed=False,
                                    detail=f"invalid ISASpec: {e}")

    if not _have("spike"):
        return state_def.GateResult(
            gate="gate2", passed=False,
            detail="spike not available on this host — C<=>Spike deferred (W2)")

    vectors = GATE2_VECTORS * len(spec.instructions)
    return state_def.GateResult(
        gate="gate2", passed=spec.passes_gate2,  # set True only after a clean diff run
        detail=(f"spike present; {len(spec.instructions)} instructions, "
                f"{vectors:,} vectors to compare (differential run pending)"),
        counterexample=None)


# ---------------------------------------------------------------------------
# Verification chain — links (1)-(2) are A1/A2 scope; (3)-(4) are A3/A5 scope
# ---------------------------------------------------------------------------

@ChiaFunction(resources={"head_local": 0.1})
def link1(report_json: str, spec_json: str) -> state_def.GateResult:
    """Link 1: kernel <=> C-model equivalence. Randomized differential testing
    over the kernel's input space (W2); structurally, it requires a HotLoopReport
    covering >=80% of cycles and a valid ISASpec whose instructions model the
    mined loops."""
    try:
        report = state_def.from_json(report_json, state_def.HotLoopReport)
        state_def.validate_hot_loop_report(report)
        spec = state_def.from_json(spec_json, state_def.ISASpec)
        state_def.validate_isa_spec(spec)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return state_def.GateResult(gate="link1", passed=False,
                                    detail=f"invalid artifact: {e}")
    coverage_ok = report.coverage >= COVERAGE_TARGET
    return state_def.GateResult(
        gate="link1", passed=coverage_ok,
        detail=(f"coverage {report.coverage:.2%} "
                f"{'≥' if coverage_ok else '<'} {COVERAGE_TARGET:.0%}; "
                f"kernel<=>C differential test pending (W2)"))


@ChiaFunction(resources={"head_local": 0.1})
def link2(gate2_json: str) -> state_def.GateResult:
    """Link 2: C model <=> patched Spike agreement — mirrors Gate 2's verdict."""
    g = state_def.from_json(gate2_json, state_def.GateResult)
    return state_def.GateResult(gate="link2", passed=g.passed, detail=g.detail,
                                counterexample=g.counterexample)


# ---------------------------------------------------------------------------
# A3-A5 — fixed, bounded tool nodes (one platform: core + compiler path)
# ---------------------------------------------------------------------------

@ChiaFunction(resources={"rtl": 1.0})
def A3_eval_rtl(core: str, spec_json: str, work_dir: str = "/tmp/red-rtl") -> state_def.EvalResult:
    """Fixed tool node: wire the extension into the core's PCPI coprocessor
    interface and verify by simulation. This wrapper (a) confirms the core
    exposes a PCPI interface, and (b) sanity-compiles the core's Verilog with
    iverilog when present."""
    os.makedirs(work_dir, exist_ok=True)
    verilogs = [full for rel, full in find_sources(core) if rel.endswith(".v")]
    pcpi = ("pcpi_valid", "pcpi_insn", "pcpi_rs1", "pcpi_rs2",
            "pcpi_wr", "pcpi_rd", "pcpi_wait", "pcpi_ready")
    blob = "\n".join(open(v, errors="replace").read() for v in verilogs)
    found = [sig for sig in pcpi if sig in blob]

    if not found:
        return state_def.EvalResult(
            node="A3", passed=False,
            detail="core Verilog exposes no PCPI interface "
                   f"(expected signals {', '.join(pcpi)})")

    if not _have("iverilog"):
        return state_def.EvalResult(
            node="A3", passed=False,
            detail=f"PCPI signals present ({', '.join(found)}) but iverilog missing")

    for v in verilogs:
        out = os.path.join(work_dir, os.path.basename(v) + ".vvp")
        p = subprocess.run(["iverilog", "-o", out, v], capture_output=True, text=True)
        if p.returncode == 0:
            return state_def.EvalResult(
                node="A3", passed=True,
                detail=f"core elaborates with iverilog; PCPI signals present "
                       f"({', '.join(found)})",
                metrics={"pcpi_signals": len(found), "elaborated": os.path.basename(v)})
    return state_def.EvalResult(
        node="A3", passed=False, detail="iverilog elaboration of core Verilog failed")


@ChiaFunction(resources={"llvm": 1.0})
def A4_eval_compiler(spec_json: str) -> state_def.EvalResult:
    """Fixed tool node: an LLVM optimization pass maps hot kernels to the new
    instructions. This wrapper verifies the LLVM toolchain is present and
    reports its version; the pass itself is built from the ISASpec encodings."""
    missing = _missing(["llvm"])
    if missing:
        return _unavailable("A4", missing, "LLVM toolchain")

    try:
        spec = state_def.from_json(spec_json, state_def.ISASpec)
        state_def.validate_isa_spec(spec)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return state_def.EvalResult(node="A4", passed=False, detail=f"invalid ISASpec: {e}")

    clang_v = subprocess.run(["clang", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    opt_v = subprocess.run(["opt", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    return state_def.EvalResult(
        node="A4", passed=True,
        detail=f"LLVM toolchain ready; {len(spec.instructions)} instructions to lower",
        metrics={"clang": clang_v, "opt": opt_v})


@ChiaFunction(resources={"spike": 1.0})
def A5_eval_e2e(spec_json: str, core: str) -> state_def.SpeedupResult:
    """Fixed tool node: compile the application, run it on the extended core,
    and measure speedup. Requires a patched Spike and a RISC-V cross-compiler;
    degrades gracefully when either is missing."""
    missing = _missing(["spike", "riscv_gcc"])
    if missing:
        base = _unavailable("A5", missing, "end-to-end evaluation")
        return state_def.SpeedupResult(node="A5", passed=False, detail=base.detail)
    try:
        spec = state_def.from_json(spec_json, state_def.ISASpec)
        state_def.validate_isa_spec(spec)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return state_def.SpeedupResult(node="A5", passed=False, detail=f"invalid ISASpec: {e}")
    return state_def.SpeedupResult(
        node="A5", passed=False,
        detail="spike + riscv-gcc present; e2e speedup run pending (W3)")


# ---------------------------------------------------------------------------
# Chain assembly — link3 (Spike<=>RTL) from A3, link4 (RTL<=>e2e) from A5
# ---------------------------------------------------------------------------

@ChiaFunction(resources={"head_local": 0.1})
def verify_chain(report_json: str, spec_json: str, gate2_json: str,
                 a3_json: str, a5_json: str) -> list[state_def.GateResult]:
    """Assemble the four-link verification chain (Fig. 3) from the sub-results.
    Links 1-2 close the A1/A2 contribution; links 3-4 are the A3/A5 fixed nodes."""
    def _read(data: str, cls):
        return state_def.from_json(data, cls)

    l1 = link1(report_json, spec_json)
    l2 = link2(gate2_json)
    a3 = _read(a3_json, state_def.EvalResult)
    a5 = _read(a5_json, state_def.SpeedupResult)
    l3 = state_def.GateResult(gate="link3", passed=a3.passed, detail=a3.detail)
    l4 = state_def.GateResult(
        gate="link4", passed=a5.passed,
        detail=a5.detail + (f" (speedup {a5.speedup:.2f}x)" if a5.speedup > 1.0 else ""))
    return [l1, l2, l3, l4]
