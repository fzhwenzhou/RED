"""Generate RTL test vectors from an ISASpec's C reference models.

Gate 2 established that each instruction's C model and the Spike ISS agree.
This script turns the *same* C models into vectors for the RTL testbench, so
eval/rtl/tb_accel.v closes the next link of the design's verification chain:
C model <=> RTL.

Output (eval/build/vectors.txt), 34 hex words per vector:

    funct3  nwords  in[0..15]  expected_out[0..15]      (zero padded)

Usage:  python eval/scripts/gen_vectors.py [ISASpec.json] [--n 200]
"""
from __future__ import annotations

import argparse
import glob
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

static void emit(int funct3, int words, const uint32_t *in, const uint32_t *out) {
    printf("%%08x\n%%08x\n", (unsigned)funct3, (unsigned)words);
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
        emit(%(f3)d, %(words)d, in, out);
    }
    for (int v = 0; v < %(n)d; v++) {
        for (int i = 0; i < %(words)d; i++) in[i] = xs32(&seed);
        memset(out, 0, sizeof out);
        %(ident)s_model(in, out);
        emit(%(f3)d, %(words)d, in, out);
    }
"""


def c_identifier(mnemonic: str) -> str:
    ident = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in mnemonic)
    return ident if not ident[:1].isdigit() else "_" + ident


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", nargs="?", default=None, help="ISASpec.json (default: newest run)")
    ap.add_argument("--n", type=int, default=200, help="random vectors per instruction")
    ap.add_argument("--out", default=os.path.join(REPO, "eval", "build", "vectors.txt"))
    args = ap.parse_args()

    spec_path = args.spec
    if spec_path is None:
        found = sorted(glob.glob(os.path.join(REPO, "output", "db", "*", "*", "ISASpec.json")))
        if not found:
            print("no ISASpec.json under output/db — run the RED loop first", file=sys.stderr)
            return 1
        spec_path = found[-1]
    spec = json.load(open(spec_path))
    print(f"spec: {spec_path}  ({spec['name']}, {len(spec['instructions'])} instructions)")

    models, calls = [], []
    for ins in spec["instructions"]:
        if ins["words"] > MAX_WORDS:
            print(f"  skipping {ins['mnemonic']}: words={ins['words']} exceeds the "
                  f"testbench buffer ({MAX_WORDS})", file=sys.stderr)
            continue
        f3 = int(ins["funct3"], 2)
        models.append(ins["c_model"])
        calls.append(CALL % {"mnemonic": ins["mnemonic"], "ident": c_identifier(ins["mnemonic"]),
                             "f3": f3, "words": ins["words"], "n": args.n})
        print(f"  {ins['mnemonic']:<10} funct3={f3} words={ins['words']}")

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
    stride = 2 + 2 * MAX_WORDS
    print(f"wrote {args.out}: {words // stride} vectors ({words} hex words)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
