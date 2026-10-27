# Performance gate

Target: 1.86x whole-application speedup.
Predicted: **2.19x** over 59.6% of profiled cycles.

The extension meets the target.

```
covers 59.6% of profiled cycles; predicted whole-application speedup 2.19x
  crc32.f8: 29 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 477 cyc of software = 16.43x
  crc16.f8: 29 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 477 cyc of software = 16.43x
  crcdnp.f8: 29 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 477 cyc of software = 16.44x
  crckermit.f8: 29 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 477 cyc of software = 16.44x
  crcccitt.f8: 29 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 545 cyc of software = 18.78x
  crc64.f8h: 53 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 409 cyc of software = 7.77x — its C model executes 170 instructions, of which 170 are not accounted for by mac_ops=0; charged as 24 sequential logic steps; invocations UNVERIFIED (~578 implied)
  crc64.f8l: 52 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 409 cyc of software = 7.79x — its C model executes 169 instructions, of which 169 are not accounted for by mac_ops=0; charged as 23 sequential logic steps; invocations UNVERIFIED (~582 implied)
  crcsick.w: 37 cyc (0 beats, 0 MAC, 0.00 MAC/word, x4,096 per call) vs 374 cyc of software = 10.11x — its C model executes 58 instructions, of which 58 are not accounted for by mac_ops=0; charged as 8 sequential logic steps

**This prediction rests on an unverified declaration.** Gate 3 measures everything else it uses — the software cost from the profile, the instruction's work from its own C model — but not this:
  - invocations=2048 is the designer's declaration and RED cannot verify it: one call of crc_64_ecma executes 98,313 host instructions against this model's 170, which is consistent with roughly 578 invocations, not 2048. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
  - invocations=2048 is the designer's declaration and RED cannot verify it: one call of crc_64_ecma executes 98,313 host instructions against this model's 169, which is consistent with roughly 582 invocations, not 2048. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
```
