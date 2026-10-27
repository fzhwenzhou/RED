# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **8.26x** over 97.2% of profiled cycles.

The extension meets the target.

```
covers 97.2% of profiled cycles; predicted whole-application speedup 8.26x
  p256.lda: 99 cyc (16 beats, 0 MAC, 0.00 MAC/word, x2 per call) vs 602 cyc of software = 6.05x — its C model executes 162 instructions, of which 162 are not accounted for by mac_ops=0; charged as 22 sequential logic steps; invocations UNVERIFIED (~1 implied)
  p256.ldr: 92 cyc (16 beats, 0 MAC, 0.00 MAC/word) vs 1204 cyc of software = 13.09x — its C model executes 108 instructions, of which 108 are not accounted for by mac_ops=0; charged as 15 sequential logic steps
  p256.str: 84 cyc (16 beats, 0 MAC, 0.00 MAC/word, x2 per call) vs 602 cyc of software = 7.15x — its C model executes 52 instructions, of which 52 are not accounted for by mac_ops=0; charged as 7 sequential logic steps
  p256.mac2: 70 cyc (0 beats, 16 MAC, 4.00 MAC/word, x4 per call) vs 2187 cyc of software = 31.36x — its C model executes 414 instructions, of which 178 are not accounted for by mac_ops=16; charged as 25 sequential logic steps
  p256.red: 264 cyc (0 beats, 0 MAC, 0.00 MAC/word, x28 per call) vs 2483 cyc of software = 9.41x — its C model executes 1,694 instructions, of which 1,694 are not accounted for by mac_ops=0; charged as 235 sequential logic steps; invocations UNVERIFIED (~5 implied)
  p256.subm: 80 cyc (0 beats, 0 MAC, 0.00 MAC/word) vs 1204 cyc of software = 15.13x — its C model executes 365 instructions, of which 365 are not accounted for by mac_ops=0; charged as 51 sequential logic steps; invocations UNVERIFIED (~0 implied)

**This prediction rests on an unverified declaration.** Gate 3 measures everything else it uses — the software cost from the profile, the instruction's work from its own C model — but not this:
  - invocations=2 is the designer's declaration and RED cannot verify it: one call of uECC_vli_modSub executes 141 host instructions against this model's 162, which is consistent with roughly 1 invocations, not 2. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
  - invocations=28 is the designer's declaration and RED cannot verify it: one call of vli_mmod_fast_secp256r1 executes 8,168 host instructions against this model's 1,694, which is consistent with roughly 5 invocations, not 28. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
  - invocations=1 is the designer's declaration and RED cannot verify it: one call of uECC_vli_modSub executes 141 host instructions against this model's 365, which is consistent with roughly 0 invocations, not 1. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
```
