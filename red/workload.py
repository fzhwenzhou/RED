"""Gate 0 — is this a workload, or just a program?

RED's input is "one application project + representative workloads". The second
half is usually missing. What a repository ships is a *unit test*, and the
difference is not a matter of degree:

    micro-ecc  test/test_ecdh.c    10,302,000,000 instructions
    libcrc     test/testall.c            272,664 instructions
    matrixmul  test.c                    125,089 instructions

The bottom two are 40,000x too small, and their profiles are the dynamic loader,
``printf`` and process startup. Mining them yields hot "loops" like
``0x0000000000010db4``; the coverage gate then fails a project whose library is
perfectly good, because nobody ever wrote the workload.

:mod:`red.loop` therefore dispatches a sub-agent that reads the repository and
writes one (``red/prompts/workload.md``). This module is the half that decides
whether it worked, and it decides by running it:

``compiles``        the harness builds against the project's own sources
``terminates``      it exits 0 within the timeout, twice
``does work``       one run executes >= WORKLOAD_MIN_IR instructions
``exercises it``    >= WORKLOAD_MIN_PROJECT_SHARE of those instructions are in
                    functions the project's binary defines, not in libc
``is a measurement`` two runs agree within WORKLOAD_DETERMINISM

The third and fourth are the ones that matter, and the fourth is the one an
agent cannot talk its way past: "the work is in the project's own code" is
decided from the *binary's symbol table*, intersected with what callgrind saw
execute. A harness that loops around ``printf`` fails it however convincing its
rationale reads.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from red import callgrind, state_def
from red.constants import (
    WORKLOAD_DETERMINISM,
    WORKLOAD_MIN_IR,
    WORKLOAD_MIN_PROJECT_SHARE,
    WORKLOAD_TIMEOUT_SECONDS,
    find_sources,
)


# ---------------------------------------------------------------------------
# What counts as "the project's own code"
# ---------------------------------------------------------------------------

def _defined_symbols(binary: str) -> set:
    """Function symbols the harness binary itself defines.

    This is the mechanical definition of "the project's own code": libc arrives
    through a shared object, so every function *defined in this binary* is the
    project's or the harness's. ``nm`` answers it exactly, with no guessing from
    names and no list of libc functions to keep up to date.
    """
    if shutil.which("nm") is None:
        return set()
    p = subprocess.run(["nm", "--defined-only", binary],
                       capture_output=True, text=True)
    out = set()
    for line in p.stdout.splitlines():
        parts = line.split()
        # "<addr> T name" / "<addr> t name" — text (code) symbols only
        if len(parts) >= 3 and parts[1] in ("T", "t", "W", "w"):
            out.add(parts[2])
    return out


def _source_defined(project: str) -> set:
    """Fallback when ``nm`` is unavailable: function names the project's own
    sources define. Weaker than the symbol table -- it cannot tell a definition
    from a prototype in every case -- so it is only used when nm is missing."""
    pattern = re.compile(
        r"^[A-Za-z_][\w\s\*]*?\b([A-Za-z_]\w*)\s*\([^;]*\)\s*\{", re.M)
    names = set()
    for _rel, full in find_sources(project):
        if not full.endswith((".c", ".h", ".inc", ".inl")):
            continue
        try:
            with open(full, errors="replace") as f:
                names |= set(pattern.findall(f.read()))
        except OSError:
            continue
    return names - {"if", "for", "while", "switch", "return", "sizeof"}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def measure(project: str, harness: str, work_dir: str, cflags: str = "",
            exclude: str = "") -> state_def.WorkloadSpec:
    """Build and run *harness* against *project*, and measure what it does.

    Returns a WorkloadSpec whose measured fields are filled in and whose
    ``detail`` says, in the words the agent will read, exactly which condition
    failed. Never raises: a harness that does not compile is a result, not an
    error.
    """
    from red import nodes                     # local: avoids a cycle at import

    spec = state_def.WorkloadSpec(project=project, entry=harness)
    os.makedirs(work_dir, exist_ok=True)

    binary, log, _rel = nodes._build_harness(project, work_dir, cflags,
                                             harness, exclude)
    if binary is None:
        spec.detail = f"the harness does not build:\n{log}"
        return spec

    # Two runs under callgrind: the counts are what "does enough work" and
    # "is deterministic" are decided from, and the second run costs the same
    # as the first only because callgrind is the slow part either way.
    totals, per_fn = [], {}
    for i in (1, 2):
        out = os.path.join(work_dir, f"workload{i}.callgrind")
        try:
            run = subprocess.run(
                ["valgrind", "--tool=callgrind", f"--callgrind-out-file={out}",
                 "--quiet", binary],
                capture_output=True, text=True,
                timeout=WORKLOAD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            spec.detail = (f"the harness did not finish within "
                           f"{WORKLOAD_TIMEOUT_SECONDS}s under callgrind. A "
                           "workload has to be profilable: aim for a few "
                           "seconds of native run time, not minutes.")
            return spec
        except OSError as exc:
            spec.detail = f"could not run the harness: {exc}"
            return spec
        spec.exit_code = run.returncode
        if run.returncode != 0:
            spec.detail = (f"the harness exited {run.returncode} (it must exit "
                           f"0):\n{(run.stdout + run.stderr).strip()[-600:]}")
            return spec
        if not os.path.exists(out):
            spec.detail = "callgrind produced no profile for the harness"
            return spec
        counts, _calls = callgrind.parse_raw(out)
        totals.append(sum(counts.values()))
        if i == 1:
            per_fn = counts
    spec.runs = totals

    spec.dynamic_ir = totals[0]
    own = _defined_symbols(binary) or _source_defined(project)
    spec.project_ir = sum(n for fn, n in per_fn.items() if fn in own)
    spec.project_share = (spec.project_ir / spec.dynamic_ir
                          if spec.dynamic_ir else 0.0)
    spec.drift = (abs(totals[0] - totals[1]) / totals[0]) if totals[0] else 1.0
    spec.api = sorted(
        (fn for fn in per_fn if fn in own),
        key=lambda fn: -per_fn[fn])[:12]

    problems = []
    if spec.dynamic_ir < WORKLOAD_MIN_IR:
        # Give the factor, not just the verdict. An agent told only "too small"
        # scales timidly and burns the round budget getting there: one run went
        # 24.8M -> 19.3M -> 39.6M against a 50M floor and never arrived, having
        # been within a factor of 1.3 the whole time. The arithmetic is free to
        # compute here and the agent should not have to guess it.
        factor = WORKLOAD_MIN_IR / max(spec.dynamic_ir, 1)
        problems.append(
            f"it executes only {spec.dynamic_ir:,} instructions, and a workload "
            f"needs at least {WORKLOAD_MIN_IR:,} — you are {factor:.1f}x short. "
            f"**Multiply the work by at least {max(2, int(factor * 2 + 0.5))}x**: "
            "raise the iteration count, the buffer size or the problem "
            "dimension, whichever scales the kernel rather than the setup. "
            "Overshooting is free; the profile just needs to be dominated by "
            "the kernel.")
    if spec.project_share < WORKLOAD_MIN_PROJECT_SHARE:
        top = ", ".join(f"{fn} {per_fn[fn] * 100.0 / spec.dynamic_ir:.0f}%"
                        for fn in sorted(per_fn, key=lambda f: -per_fn[f])[:5])
        problems.append(
            f"only {spec.project_share:.1%} of the work is in the project's own "
            f"code (needs {WORKLOAD_MIN_PROJECT_SHARE:.0%}). The hottest "
            f"functions are: {top}. Printing, formatting and allocation are not "
            "the library; call the API in a loop and keep I/O out of it.")
    if spec.drift > WORKLOAD_DETERMINISM:
        problems.append(
            f"two runs disagreed by {spec.drift:.2%} (limit "
            f"{WORKLOAD_DETERMINISM:.0%}) — the harness is not deterministic. "
            "Remove anything that reads the clock, the environment, "
            "/dev/urandom or uninitialised memory; seed any PRNG to a constant.")

    spec.passes_gate0 = not problems
    spec.detail = ("; ".join(problems) if problems else
                   f"{spec.dynamic_ir:,} instructions, {spec.project_share:.1%} "
                   f"in the project's own code, {spec.drift:.2%} run-to-run "
                   f"drift")
    return spec


def verdict(spec: state_def.WorkloadSpec) -> state_def.GateResult:
    """Gate 0's verdict on a candidate workload."""
    if spec is None:
        return state_def.GateResult(gate="gate0", passed=False,
                                    detail="no workload was produced")
    return state_def.GateResult(
        gate="gate0", passed=bool(spec.passes_gate0),
        detail=(f"{spec.dynamic_ir:,} instructions, "
                f"{spec.project_share:.1%} in the project's own code, "
                f"{spec.drift:.2%} drift"
                if spec.runs else spec.detail[:200]),
        counterexample=None if spec.passes_gate0 else spec.detail)


def render(spec: state_def.WorkloadSpec) -> str:
    """Gate 0's measurement, as the workload agent reads it back."""
    if spec is None:
        return "No workload has been measured yet."
    lines = ["# Gate 0 — workload measurement", ""]
    if not spec.runs:
        lines += ["The harness never ran.", "", "```", spec.detail[:2000], "```"]
        return "\n".join(lines)
    lines += [
        f"- instructions executed: **{spec.dynamic_ir:,}** "
        f"(minimum {WORKLOAD_MIN_IR:,})",
        f"- of which in the project's own code: **{spec.project_share:.1%}** "
        f"(minimum {WORKLOAD_MIN_PROJECT_SHARE:.0%})",
        f"- run-to-run drift: **{spec.drift:.2%}** "
        f"(limit {WORKLOAD_DETERMINISM:.0%})",
        f"- exit code: {spec.exit_code}", ""]
    if spec.api:
        lines += ["Hottest functions of the project that your harness reached:",
                  ""] + [f"- `{fn}`" for fn in spec.api[:10]] + [""]
    lines += (["**Gate 0 passes.**"] if spec.passes_gate0 else
              ["**Gate 0 fails.**", "", spec.detail])
    return "\n".join(lines)
