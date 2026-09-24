#!/usr/bin/env python3
"""Check RED's performance gate against the RTL this directory measured.

Gate 3 predicts; `eval/` measures. This script is the join between them: it
re-prices each shipped `ISASpec` with `red.cost` on that run's own frozen
`HotLoopReport` and `CoreProfile`, and prints the prediction beside the
cycle-accurate number from RESULTS.md.

It exists because the gate was wrong by a consistent factor and nobody noticed:
the three extensions built here were over-predicted by 6.3x to 7.0x, and the
causes were only separable by doing exactly this.

    ./.venv/bin/python eval/scripts/reprice.py [--runs output/work]

Anything that changes `red/cost.py` should be run through this before it is
believed. The RTL column is the only number in this repository that came from a
cycle-accurate simulation rather than from a model.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from red import cost, state_def                                  # noqa: E402

# Measured in this directory; see RESULTS.md. Whole-application speedup on
# cycle-accurate PicoRV32 RTL.
RTL = {
    "micro-ecc": (1.6748, "one secp256r1 ECDH exchange"),
    "libcrc": (2.1025, "CRC-16/32/64 over one 4 KiB buffer"),
    "matrixmul": (14.5719, "one 10x10 float matrix multiply"),
}


SPEC_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "spec")


def _inputs(proj: str, runs_root: str) -> tuple:
    """(spec, report, core, provenance) for one project.

    Prefers the **pinned** inputs in `eval/spec/`. The RTL in this directory is
    hand-written per instruction, so it implements the pinned specification and
    nothing else; pricing a later run's specification and comparing it with
    these measurements would be comparing two different designs. The report and
    core profile are pinned with it because the prediction depends on them just
    as much — the first version of this script read them from `output/work/`,
    which is regenerated on every run, so its table could not be reproduced
    once the run directory was cleared.

    Falls back to the newest run directory when nothing is pinned, which is what
    you want while iterating on a workload that has no RTL yet.
    """
    pinned_spec = os.path.join(SPEC_DIR, f"{proj}.json")
    pinned_report = os.path.join(SPEC_DIR, f"{proj}.report.json")
    pinned_core = os.path.join(SPEC_DIR, f"{proj}.core.json")
    if os.path.exists(pinned_spec) and os.path.exists(pinned_report):
        spec = state_def.from_json(open(pinned_spec).read(), state_def.ISASpec)
        report = state_def.from_json(open(pinned_report).read(),
                                     state_def.HotLoopReport)
        core = (state_def.from_json(open(pinned_core).read(),
                                    state_def.CoreProfile)
                if os.path.exists(pinned_core) else None)
        return spec, report, core, "pinned"

    runs = sorted(glob.glob(os.path.join(runs_root, proj, "*")))
    runs = [r for r in runs if os.path.exists(os.path.join(r, "ISASpec.json"))]
    if not runs:
        return None, None, None, ""
    run = runs[-1]
    spec = state_def.from_json(
        open(os.path.join(run, "ISASpec.json")).read(), state_def.ISASpec)
    report = state_def.from_json(
        open(os.path.join(run, "HotLoopReport.json")).read(),
        state_def.HotLoopReport)
    core_path = os.path.join(run, "CoreProfile.json")
    core = (state_def.from_json(open(core_path).read(), state_def.CoreProfile)
            if os.path.exists(core_path) else None)
    if os.path.exists(pinned_spec):
        pin = state_def.from_json(open(pinned_spec).read(), state_def.ISASpec)
        if {i.mnemonic for i in pin.instructions} != {i.mnemonic
                                                      for i in spec.instructions}:
            return spec, report, core, (
                f"{os.path.basename(run)} — WARNING: this is not the "
                "specification the RTL implements, so the comparison below is "
                "between two different designs")
    return spec, report, core, os.path.basename(run)


def reprice(spec, report, core, scratch: str):
    measured = cost.measure_ops(spec, scratch)
    return cost.estimate(spec, report, core, measured)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", default="output/work",
                    help="where the per-run directories live")
    ap.add_argument("--scratch", default="output/reprice")
    args = ap.parse_args()

    print(f"{'workload':11s} {'RTL':>9s} {'gate 3 model':>13s} {'error':>8s}   "
          f"coverage")
    print("-" * 62)
    errors = []
    details = []
    for proj, (rtl, what) in RTL.items():
        spec, report, core, prov = _inputs(proj, args.runs)
        if spec is None:
            print(f"{proj:11s} {rtl:8.2f}x  (nothing pinned and no run under "
                  f"{args.runs})")
            continue
        est = reprice(spec, report, core, os.path.join(args.scratch, proj))
        err = est.app_speedup / rtl
        comparable = "WARNING" not in prov
        if comparable:
            errors.append(err if err >= 1 else rtl / est.app_speedup)
        print(f"{proj:11s} {rtl:8.2f}x {est.app_speedup:12.2f}x "
              f"{err:7.2f}x   {est.covered_share:6.1%}"
              + ("" if comparable else "   (different design — not comparable)"))
        details.append((proj, what, est, spec, prov))

    print("-" * 62)
    if errors:
        print(f"mean absolute error {sum(errors)/len(errors):.2f}x, "
              f"worst {max(errors):.2f}x   "
              f"(over {len(errors)} of {len(RTL)} workloads)")
    if len(errors) < len(RTL):
        print("Rows marked 'different design' price a specification the RTL in "
              "this directory does not implement, so they say nothing about the "
              "model's accuracy. Pin that run's spec, report and core into "
              "eval/spec/ (and write the RTL for it) to make them comparable.")

    for proj, what, est, spec, prov in details:
        print(f"\n=== {proj} — {what}  [inputs: {prov}]")
        for c in est.per_instruction:
            print(f"    {c.mnemonic}: {c.cycles:.0f} cyc + {c.marshal_cycles:.0f} "
                  f"marshalling, x{c.invocations:,.0f}/call, "
                  f"replaces {c.replaced_cycles:,.0f} cyc = {c.speedup:.2f}x")
            if c.note:
                print(f"        {c.note}")
        for u in est.unverified:
            print(f"    UNVERIFIED: {u[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
