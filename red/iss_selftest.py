"""Prove Gate 2's toolchain works, using instructions whose answers are known.

``scripts/setup_spike.sh`` runs this after building Spike. It is deliberately
independent of any agent output: three hand-written instructions stand in for a
real ISASpec, and the check is that Gate 2 reaches the verdict we already know
is right —

    xoracc   C model and ISS agree            -> Gate 2 must PASS
    offbyone the ISS model is deliberately wrong on one word
                                              -> Gate 2 must FAIL, and the
                                                 counterexample must name the
                                                 first vector where they part

A Gate 2 that cannot fail is worth nothing, so the negative case matters as much
as the positive one.

    python -m red.iss_selftest
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

from red import iss, state_def

WORDS = 4

# Same arithmetic, written twice — as the designer's C model and as the ISS
# model. Agreement here exercises decode, the register ABI, the MMU traffic and
# the signature dump.
_XOR_C = """
void xoracc_model(const uint32_t in[4], uint32_t out[4]) {
    uint32_t acc = 0u;
    for (int i = 0; i < 4; i++) acc ^= in[i];
    for (int i = 0; i < 4; i++) out[i] = acc + (uint32_t)i;
}
"""
_XOR_ISS = """
void xoracc_iss(const uint32_t in[4], uint32_t out[4]) {
    uint32_t a = 0u;
    for (int k = 0; k < 4; k++) a ^= in[k];
    out[0] = a; out[1] = a + 1u; out[2] = a + 2u; out[3] = a + 3u;
}
"""

# Identical to the above except out[2], and only for inputs whose accumulator is
# odd — so the corner vectors pass and the divergence first shows up somewhere in
# the random stream, which is exactly the case the two-phase search exists for.
_BAD_C = """
void offbyone_model(const uint32_t in[4], uint32_t out[4]) {
    uint32_t acc = 0u;
    for (int i = 0; i < 4; i++) acc ^= in[i];
    for (int i = 0; i < 4; i++) out[i] = acc + (uint32_t)i;
}
"""
_BAD_ISS = """
void offbyone_iss(const uint32_t in[4], uint32_t out[4]) {
    uint32_t a = 0u;
    for (int k = 0; k < 4; k++) a ^= in[k];
    out[0] = a; out[1] = a + 1u; out[3] = a + 3u;
    out[2] = (a & 1u) ? a + 99u : a + 2u;   /* wrong for odd accumulators */
}
"""


def _insn(name, c_model, spike_model, funct7):
    return state_def.InstructionSpec(
        name=f"selftest.{name}", mnemonic=name,
        encoding=f"{funct7} rs2 rs1 000 rd 0001011",
        semantics=("acc = XOR of in[0..3]; out[i] = acc + i. "
                   "rs1 = &in[4], rs2 = &out[4]."),
        pseudocode="acc = in[0]^in[1]^in[2]^in[3]; out[i] = acc + i",
        c_model=c_model, spike_model=spike_model, words=WORDS,
        operands=["rd", "rs1", "rs2"], funct3="000", funct7=funct7,
        opcode="custom-0")


def main() -> int:
    ready, why = iss.toolchain_status()
    print(f"toolchain: {why}")
    if not ready:
        return 1

    work = tempfile.mkdtemp(prefix="red-iss-selftest-")
    failures = []
    # A smaller vector count keeps the self-test quick; the real gate runs 10^5.
    vectors = int(os.environ.get("RED_SELFTEST_VECTORS", "4096"))

    try:
        good = state_def.ISASpec(name="selftest-good",
                                 instructions=[_insn("xoracc", _XOR_C, _XOR_ISS, "0000001")])
        ok, detail, counter = iss.run_gate2(good, os.path.join(work, "good"), vectors)
        print(f"  [{'PASS' if ok else 'FAIL'}] agreeing models -> {detail}")
        if counter:
            print(f"         {counter}")
        if not ok:
            failures.append("Gate 2 rejected two models that agree")

        bad = state_def.ISASpec(name="selftest-bad",
                                instructions=[_insn("offbyone", _BAD_C, _BAD_ISS, "0000010")])
        ok, detail, counter = iss.run_gate2(bad, os.path.join(work, "bad"), vectors)
        print(f"  [{'PASS' if not ok else 'FAIL'}] disagreeing models -> {detail}")
        if counter:
            print("         " + counter.replace("\n", "\n         "))
        if ok:
            failures.append("Gate 2 accepted two models that disagree — the gate is vacuous")
        elif not counter or "vector #" not in counter:
            failures.append("Gate 2 failed without naming the divergent vector")
    finally:
        if not failures:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"  (work dir kept at {work})")

    for f in failures:
        print(f"  ERROR: {f}")
    print("iss self-test:", "PASSED" if not failures else "FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
