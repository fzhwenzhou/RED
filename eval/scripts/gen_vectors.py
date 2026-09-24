"""Generate RTL test vectors from an ISASpec's C reference models.

Gate 2 established that each instruction's C model and the Spike ISS agree.
This script turns the *same* C models into vectors for the RTL testbench, so
eval/rtl/tb_accel.v closes the next link of the design's verification chain:
C model <=> RTL.

Output (eval/build/vectors.txt), 36 hex words per vector:

    opcode  funct3  funct7  nwords  in[0..15]  expected_out[0..15]

Usage:  python eval/scripts/gen_vectors.py [ISASpec.json] [--n 200]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_WORDS = 16          # tb_accel.v's buffer stride

# Corner patterns applied to every input word, then pseudo-random vectors from
# the same xorshift32 the C-model stress harness uses, so a divergence here is
# reproducible with the numbers printed by the testbench.
HARNESS = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>

%(models)s

static uint32_t xs32(uint32_t *s) {
    uint32_t x = *s;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return *s = x;
}

static void emit(int opcode, int funct3, int funct7, int words, const uint32_t *in, const uint32_t *out) {
    printf("%%08x\n%%08x\n%%08x\n%%08x\n", (unsigned)opcode, (unsigned)funct3, (unsigned)funct7, (unsigned)words);
    for (int i = 0; i < %(maxw)d; i++) printf("%%08x\n", i < words ? in[i]  : 0u);
    for (int i = 0; i < %(maxw)d; i++) printf("%%08x\n", i < words ? out[i] : 0u);
}

int main(void) {
    uint32_t in[%(maxw)d], out[%(maxw)d];
    const uint32_t corners[] = { 0x00000000u, 0xFFFFFFFFu, 0xAAAAAAAAu, 0x55555555u,
                                 0x00000001u, 0x80000000u };
    uint32_t seed = 0xC0FFEEu;

%(calls)s
    return 0;
}
"""

CALL = r"""    /* ---- %(mnemonic)s (funct3=%(f3)d, words=%(words)d) ---- */
    for (unsigned c = 0; c < sizeof corners / sizeof corners[0]; c++) {
        for (int i = 0; i < %(words)d; i++) in[i] = corners[c];
        memset(out, 0, sizeof out);
        %(ident)s_model(in, out);
        emit(%(opcode)d, %(f3)d, %(f7)d, %(words)d, in, out);
    }
    for (int v = 0; v < %(n)d; v++) {
        for (int i = 0; i < %(words)d; i++) in[i] = xs32(&seed);
        memset(out, 0, sizeof out);
        %(ident)s_model(in, out);
        emit(%(opcode)d, %(f3)d, %(f7)d, %(words)d, in, out);
    }
"""


def c_identifier(mnemonic: str) -> str:
    ident = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in mnemonic)
    return ident if not ident[:1].isdigit() else "_" + ident


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", nargs="?", default=os.path.join(REPO, "eval", "spec", "micro-ecc.json"),
                    help="ISASpec.json (default: eval/spec/micro-ecc.json)")
    ap.add_argument("--n", type=int, default=200, help="random vectors per instruction")
    ap.add_argument("--out", default=os.path.join(REPO, "eval", "build", "vectors.txt"))
    args = ap.parse_args()

    spec_path = args.spec
    if args.n < 0:
        ap.error("--n must be nonnegative")
    spec = json.load(open(spec_path))
    print(f"spec: {spec_path}  ({spec['name']}, {len(spec['instructions'])} instructions)")

    instructions = spec["instructions"]
    if not instructions:
        ap.error("spec has no instructions")
    opcode_values = {"custom-0": 0x0B, "custom-1": 0x2B,
                     "custom-2": 0x5B, "custom-3": 0x7B}
    seen = set()
    models, calls = [], []
    for ins in instructions:
        if not 1 <= ins["words"] <= MAX_WORDS:
            ap.error(f"{ins['mnemonic']}: words must be in 1..{MAX_WORDS}")
        opcode = opcode_values.get(ins["opcode"])
        if opcode is None:
            try:
                opcode = int(ins["opcode"], 2)
            except (TypeError, ValueError):
                ap.error(f"{ins['mnemonic']}: unsupported opcode {ins['opcode']!r}")
        f3 = int(ins["funct3"], 2)
        f7 = int(ins["funct7"], 2)
        encoding = (opcode, f3, f7)
        if encoding in seen:
            ap.error(f"duplicate encoding for {ins['mnemonic']}")
        seen.add(encoding)
        models.append(ins["c_model"])
        calls.append(CALL % {"mnemonic": ins["mnemonic"], "ident": c_identifier(ins["mnemonic"]),
                             "opcode": opcode, "f3": f3, "f7": f7,
                             "words": ins["words"], "n": args.n})
        print(f"  {ins['mnemonic']:<24} opcode=0x{opcode:02x} funct3={f3} funct7={f7} words={ins['words']}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    src = os.path.join(os.path.dirname(args.out), "gen_vectors.c")
    with open(src, "w") as f:
        f.write(HARNESS % {"models": "\n".join(models), "calls": "".join(calls),
                           "maxw": MAX_WORDS})
    exe = os.path.join(os.path.dirname(args.out), "gen_vectors")
    cc = subprocess.run(["cc", "-std=c99", "-O1", "-Wall", "-o", exe, src],
                        capture_output=True, text=True)
    if cc.returncode != 0:
        print(cc.stderr[-800:], file=sys.stderr)
        return 1
    with open(args.out, "w") as f:
        run = subprocess.run([exe], stdout=f, text=True)
    if run.returncode != 0:
        return 1
    words = sum(1 for _ in open(args.out))
    stride = 4 + 2 * MAX_WORDS
    print(f"wrote {args.out}: {words // stride} vectors ({words} hex words)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
