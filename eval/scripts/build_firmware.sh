#!/usr/bin/env bash
# Build the bare-metal ECDH firmware for red_soc_tb.
#
#   bash eval/scripts/build_firmware.sh base   # unmodified micro-ecc
#   bash eval/scripts/build_firmware.sh ext    # micro-ecc using the extension
#   bash eval/scripts/build_firmware.sh micro  # per-operation microbenchmark
#
# The micro-ecc submodule is never modified: its sources are copied into
# eval/build/src-<variant>/ and, for `ext`, the patch in eval/patches/ is
# applied to that copy. Output: eval/build/<variant>.hex (+ .elf, .dis).
set -euo pipefail

VARIANT="${1:?usage: build_firmware.sh base|ext|micro|micro-ext|check}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UECC="$REPO/target_project/micro-ecc"
FW="$REPO/eval/firmware"
BUILD="$REPO/eval/build"
SRC="$BUILD/src-$VARIANT"
CROSS="${RED_RISCV_CROSS:-riscv64-unknown-elf-}"

# Pin the workload the way RED profiled it: secp256r1 only, 32-bit limbs (the
# rv32 target's natural word size).
UECC_FLAGS="-DuECC_WORD_SIZE=4 -DuECC_SUPPORTS_secp160r1=0 -DuECC_SUPPORTS_secp192r1=0
            -DuECC_SUPPORTS_secp224r1=0 -DuECC_SUPPORTS_secp256k1=0 -DuECC_SQUARE_FUNC=0"
CFLAGS="-march=rv32im -mabi=ilp32 -Os -ffreestanding -nostdlib -fno-builtin
        -ffunction-sections -fdata-sections -Wall -Wno-unused-function"

rm -rf "$SRC"; mkdir -p "$SRC" "$BUILD"
cp "$UECC"/uECC.c "$UECC"/uECC.h "$UECC"/uECC_vli.h "$UECC"/types.h \
   "$UECC"/curve-specific.inc "$UECC"/platform-specific.inc "$SRC/"

MAIN="$FW/bench.c"
if [ "$VARIANT" = check ]; then
    MAIN="$FW/check.c"
    UECC_FLAGS="$UECC_FLAGS -DuECC_ENABLE_VLI_API=1"
fi
if [ "$VARIANT" = micro ] || [ "$VARIANT" = micro-ext ]; then
    MAIN="$FW/microbench.c"
    UECC_FLAGS="$UECC_FLAGS -DuECC_ENABLE_VLI_API=1"   # the bench calls vli_* directly
fi

if [ "$VARIANT" = ext ] || [ "$VARIANT" = check ] || [ "$VARIANT" = micro-ext ]; then
    patch="$REPO/eval/patches/micro-ecc-red-ext.patch"
    [ -f "$patch" ] || { echo "missing $patch" >&2; exit 1; }
    ( cd "$SRC" && patch -p1 --no-backup-if-mismatch < "$patch" >/dev/null ) \
        || { echo "patch failed to apply" >&2; exit 1; }
    echo "applied $(basename "$patch")"
fi

"$CROSS"gcc $CFLAGS $UECC_FLAGS -I"$SRC" -I"$FW" -c "$SRC/uECC.c" -o "$BUILD/uECC.$VARIANT.o"
"$CROSS"gcc $CFLAGS $UECC_FLAGS -I"$SRC" -I"$FW" -c "$MAIN"    -o "$BUILD/bench.$VARIANT.o"
"$CROSS"gcc $CFLAGS                      -c "$FW/start.S"      -o "$BUILD/start.$VARIANT.o"
"$CROSS"gcc $CFLAGS -T "$FW/red.ld" -Wl,--gc-sections -Wl,-Map,"$BUILD/$VARIANT.map" \
    -o "$BUILD/$VARIANT.elf" \
    "$BUILD/start.$VARIANT.o" "$BUILD/bench.$VARIANT.o" "$BUILD/uECC.$VARIANT.o"

"$CROSS"objcopy -O binary "$BUILD/$VARIANT.elf" "$BUILD/$VARIANT.bin"
# makehex.py insists on whole words; pad if the image is not a multiple of 4.
sz=$(stat -c %s "$BUILD/$VARIANT.bin")
if [ $((sz % 4)) -ne 0 ]; then
    dd if=/dev/zero bs=1 count=$((4 - sz % 4)) >> "$BUILD/$VARIANT.bin" 2>/dev/null
fi
"$CROSS"objdump -d "$BUILD/$VARIANT.elf" > "$BUILD/$VARIANT.dis"
# reuse picorv32's own bin->hex converter
python3 "$REPO/target_cpu/picorv32/firmware/makehex.py" "$BUILD/$VARIANT.bin" 65536 > "$BUILD/$VARIANT.hex"

# Count real custom-0 instructions: objdump renders unknown encodings as
# ".insn 4, 0x........", and opcode = bits 6:0 = 0x0b.
n_custom=$(awk '/\.insn[ \t]+4, 0x/ {
        match($0, /0x[0-9a-f]+$/); w = substr($0, RSTART+2, RLENGTH-2);
        op = strtonum("0x" substr(w, length(w)-1)) % 128;
        if (op == 11) c++
    } END { print c+0 }' "$BUILD/$VARIANT.dis")
printf '%-9s text+data %6s bytes   custom-0 instructions: %s\n' "$VARIANT" \
  "$(stat -c %s "$BUILD/$VARIANT.bin")" "$n_custom"
