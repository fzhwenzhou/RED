# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **1.79x** over 47.3% of profiled cycles.

The extension does **not** meet the target. Per instruction:

```
covers 47.3% of profiled cycles; predicted whole-application speedup 1.79x
  secp256r1.mult: 329 cyc (32 beats, 64 MAC, 2.00 MAC/word) + 413 marshalling vs 11225 cyc of software = 15.13x
```

## How the cost is computed

    cycles = 30 + 7 x (2 x words) + 1 x mac_ops

The operand traffic is fixed by `words`, so the only way to make an instruction pay is to do **more arithmetic per operand word**. Ways to raise it, in order of effect:

1. **Fuse the loop, not the loop body.** An instruction that replaces one iteration moves its operands every iteration; one that replaces the whole loop moves them once. A 32x32 multiply-accumulate over a 5-word block does 0.1 MAC per word moved; a 256x256 multiply over a 16-word block does 2.0 — twenty times the work for the same traffic.
2. **Raise `invocations` coverage.** Check how many times your instruction must run per call of the kernel it replaces: if that number is in the tens, each invocation is paying the fixed 28.0-cycle overhead again.
3. **Drive `marshal_words` to zero.** Operands the caller has to copy into your block cost 12 cycles per word, on top of the instruction. Choose an operand layout the application already has.
4. **Cover a hotter kernel.** Amdahl caps you at the cycle share you touch; accelerating 6% of the profile perfectly yields 1.06x.
