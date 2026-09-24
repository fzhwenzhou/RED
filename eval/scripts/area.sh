#!/usr/bin/env bash
# Area of the baseline core vs the core with the RED accelerator.
#
# Two independent metrics, both from yosys with no PDK required:
#   * iCE40 LUT4 + DFF + carry counts (synth_ice40) — PicoRV32's own published
#     numbers are in iCE40 LUTs, so these are comparable to the upstream figures
#   * technology-independent cell count after generic `synth`
#
# Usage: bash eval/scripts/area.sh [outdir] [accelerator-rtl]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.."
OUT="${1:-$REPO/eval/build/area}"
ACCEL_RTL="${2:-$REPO/eval/rtl/red_accel.v}"
mkdir -p "$OUT"
RTL="$REPO/target_cpu/picorv32/picorv32.v $ACCEL_RTL $REPO/eval/rtl/red_core.v"

run() {  # run <name> <top> <accel:0|1> <flow>
    local name="$1" top="$2" accel="$3" flow="$4"
    local ys="$OUT/$name.$flow.ys"
    {
        echo "read_verilog -DSYNTHESIS $RTL"
        [ "$top" = red_core ] && echo "chparam -set ENABLE_ACCEL $accel $top"
        echo "hierarchy -top $top"
        if [ "$flow" = ice40 ]; then echo "synth_ice40 -top $top -flatten"; else echo "synth -top $top -flatten"; fi
        echo "stat"
    } > "$ys"
    yosys -q -l "$OUT/$name.$flow.log" "$ys" 2>/dev/null || {
        echo "yosys failed for $name/$flow — see $OUT/$name.$flow.log" >&2; return 1; }
}

cell() {  # cell <logfile> <cellname> -> count (0 when absent)
    local n
    n=$(grep -E "^ +[0-9]+ +$2\s*$" "$1" 2>/dev/null | tail -1 | awk '{print $1}')
    echo "${n:-0}"
}
ffs() {   # ffs <logfile> -> total flip-flops (any SB_DFF flavour)
    local n
    n=$(grep -E "^ +[0-9]+ +SB_DFF[A-Z]*\s*$" "$1" 2>/dev/null | awk '{s+=$1} END{print s}')
    echo "${n:-0}"
}
total() { local n; n=$(grep -E "^ +[0-9]+ cells\s*$" "$1" | tail -1 | awk '{print $1}'); echo "${n:-0}"; }

echo "== synthesizing (yosys $(yosys -V | awk '{print $2}')) =="
run baseline red_core   0 ice40; run baseline red_core   0 generic
run extended red_core   1 ice40; run extended red_core   1 generic
run accel    red_accel  1 ice40; run accel    red_accel  1 generic

b_lut=$(cell "$OUT/baseline.ice40.log" SB_LUT4); e_lut=$(cell "$OUT/extended.ice40.log" SB_LUT4)
a_lut=$(cell "$OUT/accel.ice40.log"    SB_LUT4)
b_dff=$(ffs "$OUT/baseline.ice40.log"); e_dff=$(ffs "$OUT/extended.ice40.log")
b_gen=$(total "$OUT/baseline.generic.log"); e_gen=$(total "$OUT/extended.generic.log")
a_gen=$(total "$OUT/accel.generic.log")

pct() { awk -v a="$1" -v b="$2" 'BEGIN{ if (b+0==0) print "n/a"; else printf "%+.1f%%", 100*(a-b)/b }'; }

printf '\n%-34s %10s %10s %10s\n' "metric" "baseline" "extended" "overhead"
printf '%-34s %10s %10s %10s\n' "iCE40 LUT4" "$b_lut" "$e_lut" "$(pct "$e_lut" "$b_lut")"
printf '%-34s %10s %10s %10s\n' "iCE40 flip-flops" "$b_dff" "$e_dff" "$(pct "$e_dff" "$b_dff")"
printf '%-34s %10s %10s %10s\n' "generic cells (tech-indep.)" "$b_gen" "$e_gen" "$(pct "$e_gen" "$b_gen")"
printf '\n%-34s %10s %10s\n' "red_accel standalone" "$a_lut" "$a_gen"
printf '%s\n' "  (LUT4 / generic cells)"

cat > "$OUT/area.json" <<JSON
{
  "tool": "yosys $(yosys -V | awk '{print $2}')",
  "baseline": {"ice40_lut4": $b_lut, "ice40_ff": $b_dff, "generic_cells": $b_gen},
  "extended": {"ice40_lut4": $e_lut, "ice40_ff": $e_dff, "generic_cells": $e_gen},
  "accel_only": {"ice40_lut4": $a_lut, "generic_cells": $a_gen}
}
JSON
echo; echo "wrote $OUT/area.json"
