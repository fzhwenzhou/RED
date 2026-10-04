# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **1.88x** over 97.2% of profiled cycles.

The extension does **not** meet the target. Per instruction:

```
covers 97.2% of profiled cycles; predicted whole-application speedup 1.88x
  secp256r1.mult: 297 cyc (32 beats, 64 MAC, 2.00 MAC/word) + 413 marshalling vs 8748 cyc of software = 12.32x — its C model executes 1,723 instructions, of which 781 are not accounted for by mac_ops=64; charged as 108 sequential logic steps
  secp256r1.mmod: 196 cyc (32 beats, 0 MAC, 0.00 MAC/word, x2 per call) + 206 marshalling vs 4374 cyc of software = 10.86x — its C model executes 514 instructions, of which 514 are not accounted for by mac_ops=0; charged as 71 sequential logic steps; invocations UNVERIFIED (~16 implied); claims to replace 4084 host instructions per invocation but its C model executes only 514 — credited on what it computes, not on what it names
  secp256r1.modsub: 158 cyc (32 beats, 0 MAC, 0.00 MAC/word) + 619 marshalling vs 1204 cyc of software = 1.55x — its C model executes 241 instructions, of which 241 are not accounted for by mac_ops=0; charged as 33 sequential logic steps

**This prediction rests on an unverified declaration.** Gate 3 measures everything else it uses — the software cost from the profile, the instruction's work from its own C model — but not this:
  - invocations=2 is the designer's declaration and RED cannot verify it: one call of vli_mmod_fast_secp256r1 executes 8,168 host instructions against this model's 514, which is consistent with roughly 16 invocations, not 2. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
```

## How the cost is computed

    cycles = 29 + 3 x (2 x words) + 1 x mac_ops

The operand traffic is fixed by `words`, so the only way to make an instruction pay is to do **more arithmetic per operand word**. Ways to raise it, in order of effect:

1. **Fuse the loop, not the loop body.** An instruction that replaces one iteration moves its operands every iteration; one that replaces the whole loop moves them once. A 32x32 multiply-accumulate over a 5-word block does 0.1 MAC per word moved; a 256x256 multiply over a 16-word block does 2.0 — twenty times the work for the same traffic.
2. **Raise `invocations` coverage.** Check how many times your instruction must run per call of the kernel it replaces: if that number is in the tens, each invocation is paying the fixed 28.0-cycle overhead again.
3. **Drive `marshal_words` to zero.** Operands the caller has to copy into your block cost 10 cycles per word, on top of the instruction. Choose an operand layout the application already has.
4. **Cover a hotter kernel.** Amdahl caps you at the cycle share you touch; accelerating 6% of the profile perfectly yields 1.06x.
