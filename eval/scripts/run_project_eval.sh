#!/usr/bin/env bash
set -euo pipefail
PROJECT="${1:?usage: run_project_eval.sh matrixmul|libcrc}"
[[ "$PROJECT" = matrixmul || "$PROJECT" = libcrc ]] || { echo "unsupported project: $PROJECT" >&2; exit 2; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$REPO"
B="eval/build/$PROJECT"; SPEC="eval/spec/$PROJECT.json"; ACCEL="eval/projects/$PROJECT/red_accel.v"
mkdir -p "$B/logs"
say(){ printf '\n==> %s\n' "$*"; }
run_sim(){ local variant="$1"; "$B/obj_$variant/sim_$variant" +firmware="$B/$variant.hex" >"$B/logs/$variant.log" 2>&1; cat "$B/logs/$variant.log"; grep -q '^RESULT: PASS$' "$B/logs/$variant.log"; grep -q '\[sim\] finished: status=0 ' "$B/logs/$variant.log"; }

say "$PROJECT: C model <=> RTL"
./.venv/bin/python eval/scripts/gen_vectors.py "$SPEC" --out "$B/vectors.txt" | tee "$B/logs/vectors.log"
iverilog -g2012 -o "$B/tb.vvp" eval/rtl/tb_accel.v "$ACCEL"
vvp "$B/tb.vvp" +vectors="$B/vectors.txt" | tee "$B/logs/rtl.log"
grep -q '^tb_accel: PASS$' "$B/logs/rtl.log"; ! grep -q FAIL "$B/logs/rtl.log"
if [[ "$PROJECT" = matrixmul ]]; then
    # Check the Verilator DPI float path against the same arbitrary bit patterns.
    verilator --binary --timing --language 1800-2017 --top-module tb_accel \
      -Wno-fatal -Wno-SHORTREAL -Wno-WIDTHTRUNC -Wno-WIDTHEXPAND \
      -Wno-UNUSEDSIGNAL -Wno-DECLFILENAME --Mdir "$B/obj_tb_verilator" \
      -o tb_sim eval/rtl/tb_accel.v "$ACCEL" eval/projects/matrixmul/f32_dpi.cpp \
      >"$B/logs/build-tb-verilator.log" 2>&1
    "$B/obj_tb_verilator/tb_sim" +vectors="$B/vectors.txt" | tee "$B/logs/rtl-verilator.log"
    grep -q '^tb_accel: PASS$' "$B/logs/rtl-verilator.log"
fi

say "$PROJECT: build firmware and cores"
for variant in base ext; do bash eval/scripts/build_project_firmware.sh "$PROJECT" "$variant"; done
RTL=(eval/rtl/red_soc_tb.v eval/rtl/red_core.v "$ACCEL" target_cpu/picorv32/picorv32.v)
EXTRA=(); [[ "$PROJECT" = matrixmul ]] && EXTRA=(--language 1800-2017 -Wno-SHORTREAL eval/projects/matrixmul/f32_dpi.cpp)
for item in base:0 ext:1; do
    variant=${item%%:*}; enabled=${item##*:}
    verilator --binary -j 4 --top-module red_soc_tb -GENABLE_ACCEL="$enabled" \
      -Wno-fatal -Wno-WIDTHTRUNC -Wno-WIDTHEXPAND -Wno-UNUSEDSIGNAL \
      -Wno-DECLFILENAME -Wno-MULTIDRIVEN --Mdir "$B/obj_$variant" \
      -o "sim_$variant" "${RTL[@]}" "${EXTRA[@]}" >"$B/logs/build-$variant.log" 2>&1 \
      || { cat "$B/logs/build-$variant.log" >&2; exit 1; }
done

say "$PROJECT: end-to-end baseline vs extension"
run_sim base; run_sim ext
bc=$(awk '/cycles TOTAL/{print $NF}' "$B/logs/base.log"); ec=$(awk '/cycles TOTAL/{print $NF}' "$B/logs/ext.log")
bs=$(awk '/signature/{print $3}' "$B/logs/base.log"); es=$(awk '/signature/{print $3}' "$B/logs/ext.log")
[[ "$bc" =~ ^[1-9][0-9]*$ && "$ec" =~ ^[1-9][0-9]*$ && -n "$bs" && "$bs" = "$es" ]] || { echo "invalid or mismatched results" >&2; exit 1; }
printf '\n%-16s %12s %12s\n' '' baseline extended
printf '%-16s %12s %12s\n' cycles "$bc" "$ec"
printf '%-16s %12s %12s\n' signature "$bs" "$es"
awk -v b="$bc" -v e="$ec" 'BEGIN{printf "speedup: %.4fx (%+.1f%%)\n",b/e,100*(b-e)/b}'
