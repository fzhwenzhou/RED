#!/usr/bin/env bash
set -euo pipefail
PROJECT="${1:?usage: build_project_firmware.sh matrixmul|libcrc base|ext}"
VARIANT="${2:?usage: build_project_firmware.sh matrixmul|libcrc base|ext}"
[[ "$PROJECT" = matrixmul || "$PROJECT" = libcrc ]] || exit 2
[[ "$VARIANT" = base || "$VARIANT" = ext ]] || exit 2
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
B="$REPO/eval/build/$PROJECT"; FW="$REPO/eval/firmware"; P="$REPO/eval/projects/$PROJECT"
CROSS="${RED_RISCV_CROSS:-riscv64-unknown-elf-}"
CFLAGS="-march=rv32im -mabi=ilp32 -Os -ffreestanding -nostdlib -fno-builtin -ffunction-sections -fdata-sections -Wall"
mkdir -p "$B"
EXT=0; [[ "$VARIANT" = ext ]] && EXT=1
"$CROSS"gcc $CFLAGS -DRED_EXT=$EXT -I"$P/include" -I"$P" -I"$REPO/target_project/libcrc/include" -c "$P/bench.c" -o "$B/bench-$VARIANT.o"
"$CROSS"gcc $CFLAGS -c "$FW/start.S" -o "$B/start-$VARIANT.o"
OBJS=("$B/start-$VARIANT.o" "$B/bench-$VARIANT.o")
if [[ "$PROJECT" = libcrc && "$VARIANT" = base ]]; then
  for src in crc16 crc32 crc64; do
    "$CROSS"gcc $CFLAGS -I"$P/include" -I"$REPO/target_project/libcrc/include" -c "$REPO/target_project/libcrc/src/$src.c" -o "$B/$src.o"
    OBJS+=("$B/$src.o")
  done
fi
"$CROSS"gcc $CFLAGS -T "$FW/red.ld" -Wl,--gc-sections -o "$B/$VARIANT.elf" "${OBJS[@]}" -lgcc
"$CROSS"objcopy -O binary "$B/$VARIANT.elf" "$B/$VARIANT.bin"
sz=$(stat -c %s "$B/$VARIANT.bin"); if (( sz % 4 )); then dd if=/dev/zero bs=1 count=$((4-sz%4)) >> "$B/$VARIANT.bin" 2>/dev/null; fi
python3 "$REPO/target_cpu/picorv32/firmware/makehex.py" "$B/$VARIANT.bin" 65536 > "$B/$VARIANT.hex"
"$CROSS"objdump -d "$B/$VARIANT.elf" > "$B/$VARIANT.dis"
printf '%-10s %-4s %6s bytes\n' "$PROJECT" "$VARIANT" "$(stat -c %s "$B/$VARIANT.bin")"
