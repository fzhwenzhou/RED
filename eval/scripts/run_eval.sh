#!/usr/bin/env bash
# Full RED extension evaluation: correctness, end-to-end cycles, per-operation
# cost. Area is separate (bash eval/scripts/area.sh) because it needs no sim.
#
#   bash eval/scripts/run_eval.sh          # everything (~8 min)
#   bash eval/scripts/run_eval.sh quick    # skip the two full-ECDH runs (~1 min)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
B=eval/build
MODE="${1:-full}"
RTL="eval/rtl/red_soc_tb.v eval/rtl/red_core.v eval/rtl/red_accel.v target_cpu/picorv32/picorv32.v"
VFLAGS="-Wno-fatal -Wno-WIDTHTRUNC -Wno-WIDTHEXPAND -Wno-UNUSEDSIGNAL -Wno-DECLFILENAME -Wno-MULTIDRIVEN"
PY="${RED_VENV:-$REPO/.venv}/bin/python"; [ -x "$PY" ] || PY=python3
mkdir -p $B

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1. verify red_accel against the ISASpec C models (link 3)"
"$PY" eval/scripts/gen_vectors.py >/dev/null
iverilog -g2005-sv -o $B/tb_accel.vvp eval/rtl/tb_accel.v eval/rtl/red_accel.v
vvp $B/tb_accel.vvp +vectors=$B/vectors.txt | grep -E "pass |errors|latency|tb_accel:"

say "2. build the two cores"
for v in base:0 ext:1; do
    n=${v%%:*}; a=${v##*:}
    verilator --binary -j 4 --top-module red_soc_tb -GENABLE_ACCEL=$a $VFLAGS \
        --Mdir $B/obj_$n -o red_sim_$n $RTL 2>&1 | grep -E "%Error" || true
done
echo "  baseline core: ENABLE_ACCEL=0    extended core: ENABLE_ACCEL=1"

say "3. build the firmware images"
for v in base ext check micro micro-ext; do
    bash eval/scripts/build_firmware.sh $v 2>&1 | grep -vE "RWX permissions|^applied"
done

say "4. correctness of the patched micro-ecc (uECC_vli_mult vs a plain-C reference)"
./$B/obj_ext/red_sim_ext +firmware=$B/check.hex | grep -E "RESULT|MISMATCH|matches"

say "5. per-operation cost on the RTL"
echo "-- against unmodified micro-ecc --"
./$B/obj_ext/red_sim_ext +firmware=$B/micro.hex | grep "cycles/op"
echo "-- against patched micro-ecc --"
./$B/obj_ext/red_sim_ext +firmware=$B/micro-ext.hex | grep "cycles/op" | grep mult

if [ "$MODE" = quick ]; then
    say "skipping the end-to-end ECDH runs (quick mode)"; exit 0
fi

say "6. end-to-end: one full ECDH exchange, baseline vs extended (~3 min each)"
base_out=$(./$B/obj_base/red_sim_base +firmware=$B/base.hex)
ext_out=$(./$B/obj_ext/red_sim_ext   +firmware=$B/ext.hex)
bc_=$(echo "$base_out" | awk '/cycles TOTAL/{print $NF}')
ec_=$(echo "$ext_out"  | awk '/cycles TOTAL/{print $NF}')
br_=$(echo "$base_out" | awk '/RESULT/{print $2}')
er_=$(echo "$ext_out"  | awk '/RESULT/{print $2}')
bs_=$(echo "$base_out" | awk '/shared secret/{print $4}')
es_=$(echo "$ext_out"  | awk '/shared secret/{print $4}')

printf '\n%-22s %14s %14s\n' "" "baseline" "extended"
printf '%-22s %14s %14s\n' "cycles (whole app)" "$bc_" "$ec_"
printf '%-22s %14s %14s\n' "self-check" "$br_" "$er_"
printf '%-22s %14s %14s\n' "secret matches base" "-" \
       "$([ "$bs_" = "$es_" ] && echo yes || echo NO)"
awk -v b="$bc_" -v e="$ec_" 'BEGIN{printf "\nspeedup: %.4fx  (%+.1f%%)\n", b/e, 100*(b-e)/b}'
echo "area: run  bash eval/scripts/area.sh"
