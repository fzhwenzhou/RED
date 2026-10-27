# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **1.98x** over 68.3% of profiled cycles.

The extension does **not** meet the target. Per instruction:

```
covers 68.3% of profiled cycles; predicted whole-application speedup 1.98x
  xfop.lo: 31 cyc (0 beats, 1 MAC, 0.25 MAC/word, x250 per call) vs 238 cyc of software = 7.67x
  xfop.hi: 34 cyc (0 beats, 3 MAC, 0.75 MAC/word, x167 per call) vs 356 cyc of software = 10.40x
  xfop.rd: 43 cyc (0 beats, 0 MAC, 0.00 MAC/word, x70 per call) vs 849 cyc of software = 19.93x — its C model executes 91 instructions, of which 91 are not accounted for by mac_ops=0; charged as 13 sequential logic steps
```

## How the cost is computed

    cycles = 30 + 3 x (2 x words) + 1 x mac_ops

The operand traffic is fixed by `words`, so the only way to make an instruction pay is to do **more arithmetic per operand word**. Ways to raise it, in order of effect:

1. **Fuse the loop, not the loop body.** An instruction that replaces one iteration moves its operands every iteration; one that replaces the whole loop moves them once. A 32x32 multiply-accumulate over a 5-word block does 0.1 MAC per word moved; a 256x256 multiply over a 16-word block does 2.0 — twenty times the work for the same traffic.
2. **Raise `invocations` coverage.** Check how many times your instruction must run per call of the kernel it replaces: if that number is in the tens, each invocation is paying the fixed 28.0-cycle overhead again.
3. **Drive `marshal_words` to zero.** Operands the caller has to copy into your block cost 10 cycles per word, on top of the instruction. Choose an operand layout the application already has.
4. **Cover a hotter kernel.** Amdahl caps you at the cycle share you touch; accelerating 6% of the profile perfectly yields 1.06x.
