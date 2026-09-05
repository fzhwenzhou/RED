"""ChiaFunction workers for the RED loop — the programmatic evaluation edges.

Scope: **A1 kernel mining + A2 ISA synthesis** (the dashed enclosure of Fig. 1
in the RED design) — the research contribution, and all this repo implements.
The agentic halves live in :mod:`red.loop`; each is bounded by a programmatic
gate defined here. The downstream evaluation nodes of the full-stack figure
(A3 RTL, A4 LLVM, A5 end-to-end) are **out of scope**: RED's deliverable is the
ISASpec they would consume.

The graph the loop drives:

    A1_profile_workload (x2, profile node) ─┐
                                            ├─ gate1 (±5% re-profile)
    A1 mining agent (loop.py) ──────────────┴──> HotLoopReport
    A2 synthesis agent (loop.py, S2a->S2b->S2c) ─┐
                                                 ├─ link1 (C models build +
                                                 │         stress-run)
    ISS implementer agent (loop.py) ─────────────┤
                                                 ├─ gate2 (C model <=> patched
                                                 │         Spike, 10^5 vectors)
                                                 └─ link2 (mirrors gate2)
                                                 └──> ISASpec  (final artifact)

Every node degrades gracefully: a missing tool returns a typed result whose
``detail`` says "not available" rather than crashing.

All inputs (project dir, workload name, cflags) are parameters — RED is
general-purpose; nothing about any specific workload is hard-coded.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Optional

from chia.base.ChiaFunction import ChiaFunction

from red import state_def
from red.constants import (
    COVERAGE_TARGET,
    EXPECTED_TOOLS,
    GATE1_TOLERANCE,
    GATE2_VECTORS,
    LINK1_VECTORS,
    OUTPUT_ROOT,
    find_sources,
)


# ---------------------------------------------------------------------------
# Toolchain probing
# ---------------------------------------------------------------------------

def _have(tool: str) -> bool:
    """Is *tool* runnable here?

    Searches the Gate-2 prefix as well as PATH: scripts/setup_spike.sh installs
    spike (and, on a host without root, dtc) under RED_SPIKE_PREFIX, which the
    caller's PATH usually does not carry."""
    if shutil.which(tool) is not None:
        return True
    from red import iss
    return shutil.which(tool, path=iss.spike_env()["PATH"]) is not None


def _toolchain() -> dict:
    """Probe EXPECTED_TOOLS and return {group: {tool: bool}}."""
    return {group: {t: _have(t) for t in tools}
            for group, tools in EXPECTED_TOOLS.items()}


# ---------------------------------------------------------------------------
# A1 — kernel mining: the mechanical profiling half (the ranking half is the
# mining agent in red.loop)
# ---------------------------------------------------------------------------

def _c_sources(project: str) -> list[str]:
    return [full for rel, full in find_sources(project) if rel.endswith(".c")]


def _has_main(path: str) -> bool:
    return bool(re.search(r"\bint\s+main\s*\(",
                          open(path, errors="replace").read()))


def _build_harness(project: str, work_dir: str, cflags: str,
                   harness: str = "") -> tuple[Optional[str], str, str]:
    """Compile the project into a runnable native harness for the workload.

    *harness* names the translation unit holding ``main`` — project-relative or
    absolute. It is what actually defines the workload, so the operator passes
    it explicitly (``--harness``); a project shipping several test binaries
    would otherwise be profiled on whichever one the directory walk happened to
    reach first. Without it, the alphabetically first TU with a ``main`` is used
    so at least the choice is deterministic and reported.

    The remaining ``.c`` files are linked in at -O2, minus any that define their
    own ``main`` (test suites ship one per file — linking them would collide).
    ``cflags`` is the operator's compile input. Returns
    (binary | None, command_or_error, harness_main)."""
    os.makedirs(work_dir, exist_ok=True)
    sources = sorted(_c_sources(project))
    if not sources:
        return None, "no .c sources found under project", ""

    if harness:
        main = harness if os.path.isabs(harness) else os.path.join(project, harness)
        if not os.path.isfile(main):
            return None, f"harness {harness!r} not found under {project}", ""
    else:
        main = next((s for s in sources if _has_main(s)), None)
        if main is None:
            return None, "no int main() found among sources (no runnable harness)", ""

    main = os.path.abspath(main)
    rest = [s for s in sources if os.path.abspath(s) != main and not _has_main(s)]
    inc = " ".join(f"-I{d}" for d in sorted({os.path.dirname(s) for s in sources}))
    out = os.path.join(work_dir, "harness")
    cmd = f"cc -O2 {cflags} {inc} -o {out} {main} {' '.join(rest)}"
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    rel = os.path.relpath(main, project)
    if p.returncode != 0 or not os.path.exists(out):
        return None, f"harness build failed: {p.stderr[-800:]}", rel
    return out, cmd, rel


def _callgrind_profile(binary: str, work_dir: str) -> tuple[dict, str, str]:
    """Run the harness under callgrind and return per-function instruction counts.

    Returns ({} , "", "") when callgrind is unavailable or produced nothing, so
    the caller can fall back to the static estimate."""
    if not (_have("valgrind") and _have("callgrind_annotate")):
        return {}, "", ""
    out = os.path.join(work_dir, "callgrind.out")
    cmd = (f"valgrind --tool=callgrind --callgrind-out-file={out} "
           f"--quiet {binary} >/dev/null 2>&1")
    subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if not os.path.exists(out):
        return {}, "", ""
    annotate = subprocess.run(["callgrind_annotate", out],
                              capture_output=True, text=True)
    counts = _parse_callgrind(annotate.stdout)
    return (counts, "callgrind", cmd) if counts else ({}, "", "")


def _perf_cycles(binary: str) -> int:
    """Total hardware cycles for one harness run, or 0 when perf can't count
    them (missing, or no PMU access — the common case in a VM/container). Used
    only to corroborate the callgrind instruction counts."""
    if not _have("perf"):
        return 0
    p = subprocess.run(f"perf stat -e cycles {binary} >/dev/null", shell=True,
                       capture_output=True, text=True)
    m = re.search(r"([\d,]+)\s+cycles", p.stderr or "")
    return int(m.group(1).replace(",", "")) if m else 0


def _parse_callgrind(annotate: str) -> dict:
    """Map function name -> self instruction count (Ir) from callgrind_annotate.

    Its per-function rows look like::

        2,339,300 (38.81%)  ???:uECC_vli_mult [/path/to/harness]
          194,328 ( 3.22%)  ???:uECC_vli_mmod'2 [/path/to/harness]

    The percentage column is absent in some versions, the ``file:`` prefix is
    ``???`` without debug info, and callgrind appends ``'2``/``'3`` to a
    function entered recursively — those rows are folded back into the base
    function so one function is one entry. Rows without a ``[object]`` field
    (``PROGRAM TOTALS``, headers) are not per-function rows and are skipped."""
    counts: dict = {}
    row = re.compile(r"^\s*([\d,]+)\s+(?:\([\s\d.]+%\)\s+)?(\S+)\s+\[")
    for line in annotate.splitlines():
        m = row.match(line)
        if not m:
            continue
        fn = m.group(2).rsplit(":", 1)[-1].split("'")[0]
        counts[fn] = counts.get(fn, 0) + int(m.group(1).replace(",", ""))
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
                        cflags: str = "", harness: str = "") -> dict:
    """Build + run the domain harness and profile it dynamically (callgrind),
    corroborated by perf's cycle count. Returns a dict the mining agent reads
    via the ProfileTool:

        {project, workload, profile_method, cycle_counts, perf_cycles,
         available, command, harness_built, tools}

    Degrades gracefully: without callgrind it falls back to *static* per
    function assembly instruction counts (``profile_method="static-asm"``,
    a much weaker signal); without any compiler it reports
    ``profile_method="none"`` with empty counts."""
    tc = _toolchain()
    available = sorted(g for g, t in tc.items() if any(t.values()))
    method, counts, command, harness_built, perf_cycles = "none", {}, "", False, 0
    harness_main, build_log = "", ""

    if _have("cc"):
        binary, build_log, harness_main = _build_harness(project, work_dir,
                                                         cflags, harness)
        if binary:
            harness_built = True
            command = build_log
            counts, method, cmd = _callgrind_profile(binary, work_dir)
            if counts:
                command = cmd
            perf_cycles = _perf_cycles(binary)
        if not counts:
            counts, method, command = _static_profile(project, work_dir, cflags)

    return {
        "project": project,
        "workload": workload,
        "profile_method": method,
        "cycle_counts": counts,
        "perf_cycles": perf_cycles,
        "available": available,
        "command": command,
        "harness_main": harness_main,
        "harness_built": harness_built,
        "build_log": "" if harness_built else build_log,
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
# A2 — ISA synthesis: link 1 and Gate 2 (the S2a/S2b/S2c agents live in
# red.loop)
# ---------------------------------------------------------------------------

# The harness link 1 compiles around each instruction's C model. It runs the
# model on corner vectors (all-zeros, all-ones, and two alternating bit
# patterns) plus LINK1_VECTORS pseudo-random ones, calling twice per vector to
# catch a model whose output depends on anything but its inputs. Built under
# ASan+UBSan with -fno-sanitize-recover, so an out-of-bounds write or any
# undefined behaviour aborts the run and fails the link with a counterexample
# instead of silently "passing".
_HARNESS = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
%(model)s
/* ---- end model ---- */

#define WORDS %(words)d
#define NRAND %(nrand)d

static uint32_t xs32(uint32_t *s) {
    uint32_t x = *s;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return *s = x;
}

static int run_vector(const uint32_t *in, long idx) {
    uint32_t a[WORDS], b[WORDS];
    memset(a, 0, sizeof a);
    memset(b, 0, sizeof b);
    %(ident)s_model(in, a);
    %(ident)s_model(in, b);
    if (memcmp(a, b, sizeof a) != 0) {
        printf("FAIL vector %%ld: model is not a pure function of its input\n", idx);
        return 1;
    }
    return 0;
}

int main(void) {
    uint32_t in[WORDS];
    const uint32_t corners[] = { 0x00000000u, 0xFFFFFFFFu, 0xAAAAAAAAu, 0x55555555u };
    long n = 0;

    for (unsigned c = 0; c < sizeof corners / sizeof corners[0]; c++, n++) {
        for (int w = 0; w < WORDS; w++) in[w] = corners[c];
        if (run_vector(in, n)) return 1;
    }
    uint32_t seed = 0xC0FFEEu;
    for (long i = 0; i < NRAND; i++, n++) {
        for (int w = 0; w < WORDS; w++) in[w] = xs32(&seed);
        if (run_vector(in, n)) return 1;
    }
    printf("PASS %%ld vectors\n", n);
    return 0;
}
"""


def _stress_c_model(ins: state_def.InstructionSpec, work_dir: str,
                    vectors: int) -> tuple[bool, str]:
    """Build and stress-run one instruction's C reference model.

    Returns (passed, detail). Fails when the model does not compile, is not a
    pure function of its input, trips a sanitizer (out-of-bounds, overflow,
    other UB), crashes, or hangs — each of which is a concrete counterexample
    to the instruction's claimed semantics."""
    ident = state_def.c_identifier(ins.mnemonic)
    src = os.path.join(work_dir, f"{ident}_link1.c")
    binary = os.path.join(work_dir, f"{ident}_link1")
    with open(src, "w") as f:
        f.write(_HARNESS % {"model": ins.c_model, "words": ins.words,
                            "nrand": vectors, "ident": ident})

    cc = subprocess.run(
        ["cc", "-std=c99", "-O1", "-Wall", "-Wextra",
         "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
         "-o", binary, src],
        capture_output=True, text=True)
    if cc.returncode != 0:
        return False, f"c_model does not compile: {cc.stderr.strip()[-600:]}"

    try:
        run = subprocess.run([binary], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "c_model did not terminate within 300s"
    if run.returncode != 0:
        detail = (run.stdout + run.stderr).strip()[-600:]
        return False, f"c_model failed at runtime (exit {run.returncode}): {detail}"
    return True, run.stdout.strip()


@ChiaFunction(resources={"head_local": 0.1})
def link1(report_json: str, spec_json: str,
          work_dir: str = "") -> state_def.GateResult:
    """Link 1: the mined kernels' coverage holds AND every instruction's C
    reference model is executable.

    Two conditions, both mechanical:

    1. the HotLoopReport the spec was designed from covers >=COVERAGE_TARGET of
       dynamic cycles, and
    2. each instruction's ``c_model`` compiles clean and survives a
       LINK1_VECTORS-vector stress run under ASan+UBSan (see
       :func:`_stress_c_model`).

    NOTE this is *not* the full link 1 of the design, which additionally proves
    the C model equivalent to the original kernel by differential testing over
    the kernel's input space. That needs a per-kernel extraction harness and is
    not implemented — see LIMITATIONS in README.md. What runs here is real, but
    it falsifies the model's executability and totality, not its equivalence to
    the kernel."""
    try:
        report = state_def.from_json(report_json, state_def.HotLoopReport)
        state_def.validate_hot_loop_report(report)
        spec = state_def.from_json(spec_json, state_def.ISASpec)
        state_def.validate_isa_spec(spec)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return state_def.GateResult(gate="link1", passed=False,
                                    detail=f"invalid artifact: {e}")

    coverage_ok = report.coverage >= COVERAGE_TARGET
    coverage_detail = (f"coverage {report.coverage:.2%} "
                       f"{'>=' if coverage_ok else '<'} {COVERAGE_TARGET:.0%}")

    if not _have("cc"):
        return state_def.GateResult(
            gate="link1", passed=False,
            detail=f"{coverage_detail}; no C compiler on this host — "
                   "cannot build the instruction C models")

    work_dir = work_dir or os.path.join(OUTPUT_ROOT, "link1")
    os.makedirs(work_dir, exist_ok=True)
    failures = []
    for ins in spec.instructions:
        ok, detail = _stress_c_model(ins, work_dir, LINK1_VECTORS)
        if not ok:
            failures.append(f"{ins.mnemonic}: {detail}")

    passed = coverage_ok and not failures
    n = len(spec.instructions)
    return state_def.GateResult(
        gate="link1", passed=passed,
        detail=(f"{coverage_detail}; {n - len(failures)}/{n} C models built and "
                f"survived {LINK1_VECTORS:,} vectors under ASan+UBSan"),
        counterexample="\n".join(failures) or None)


@ChiaFunction(resources={"spike": 1.0})
def gate2(spec_json: str, work_dir: str = "") -> state_def.GateResult:
    """Gate 2: the ISASpec's C model and a patched Spike ISS must agree on
    GATE2_VECTORS random + corner vectors.

    Each instruction carries two executable models written independently from
    the same spec — the designer's ``c_model`` and the ISS implementer's
    ``spike_model``. :mod:`red.iss` compiles the latter into a Spike extension,
    builds a bare-metal rv32 program that actually executes the custom
    instruction, and diffs the two. A disagreement is a counterexample naming
    the exact vector, so it can be handed back to the agents.

    Unlike link 1, this exercises the *encoding*: the instruction is decoded
    from the rv32 stream, its operands arrive in architectural registers and its
    memory traffic goes through the simulator's MMU."""
    try:
        spec = state_def.from_json(spec_json, state_def.ISASpec)
        state_def.validate_isa_spec(spec)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return state_def.GateResult(gate="gate2", passed=False,
                                    detail=f"invalid ISASpec: {e}")

    from red import iss                      # imported here: only this node needs it
    work_dir = work_dir or os.path.join(OUTPUT_ROOT, "gate2")
    passed, detail, counterexample = iss.run_gate2(spec, work_dir, GATE2_VECTORS)
    return state_def.GateResult(gate="gate2", passed=passed, detail=detail,
                                counterexample=counterexample)


@ChiaFunction(resources={"head_local": 0.1})
def link2(gate2_json: str) -> state_def.GateResult:
    """Link 2: C model <=> patched Spike agreement — mirrors Gate 2's verdict."""
    g = state_def.from_json(gate2_json, state_def.GateResult)
    return state_def.GateResult(gate="link2", passed=g.passed, detail=g.detail,
                                counterexample=g.counterexample)


