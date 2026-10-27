# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **4.25x** over 100.0% of profiled cycles.

The extension meets the target.

```
covers 100.0% of profiled cycles; predicted whole-application speedup 4.25x
  xcrc32.d: 74 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 477 cyc of software = 6.44x — its C model executes 332 instructions, of which 332 are not accounted for by mac_ops=0; charged as 46 sequential logic steps; invocations UNVERIFIED (~345 implied)
  xcrc64.dh: 67 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 409 cyc of software = 6.09x — its C model executes 282 instructions, of which 282 are not accounted for by mac_ops=0; charged as 39 sequential logic steps; invocations UNVERIFIED (~349 implied)
  xcrc64.dl: 67 cyc (0 beats, 0 MAC, 0.00 MAC/word, x2,048 per call) vs 409 cyc of software = 6.10x — its C model executes 281 instructions, of which 281 are not accounted for by mac_ops=0; charged as 39 sequential logic steps; invocations UNVERIFIED (~350 implied)

**This prediction rests on an unverified declaration.** Gate 3 measures everything else it uses — the software cost from the profile, the instruction's work from its own C model — but not this:
  - invocations=2048 is the designer's declaration and RED cannot verify it: one call of crc_32 executes 114,696 host instructions against this model's 332, which is consistent with roughly 345 invocations, not 2048. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
  - invocations=2048 is the designer's declaration and RED cannot verify it: one call of crc_64_ecma executes 98,313 host instructions against this model's 282, which is consistent with roughly 349 invocations, not 2048. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
  - invocations=2048 is the designer's declaration and RED cannot verify it: one call of crc_64_ecma executes 98,313 host instructions against this model's 281, which is consistent with roughly 350 invocations, not 2048. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
```
