# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **3.67x** over 99.8% of profiled cycles.

The extension meets the target.

```
covers 99.8% of profiled cycles; predicted whole-application speedup 3.67x
  crc32.step64b: 569 cyc (34 beats, 0 MAC, 0.00 MAC/word, x524 per call) + 464 marshalling vs 3811 cyc of software = 3.69x — its C model executes 3,163 instructions, of which 3,163 are not accounted for by mac_ops=0; charged as 438 sequential logic steps; invocations UNVERIFIED (~74 implied)

**This prediction rests on an unverified declaration.** Gate 3 measures everything else it uses — the software cost from the profile, the instruction's work from its own C model — but not this:
  - invocations=524 is the designer's declaration and RED cannot verify it: one call of crc_32 executes 234,634 host instructions against this model's 3,163, which is consistent with roughly 74 invocations, not 524. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
```
