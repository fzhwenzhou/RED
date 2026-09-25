# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **7.51x** over 99.2% of profiled cycles.

The extension meets the target.

```
covers 99.2% of profiled cycles; predicted whole-application speedup 7.51x
  fdot2_custom: 85 cyc (10 beats, 2 MAC, 0.20 MAC/word, x48 per call) + 129 marshalling vs 1692 cyc of software = 7.91x — its C model executes 50 instructions, of which 21 are not accounted for by mac_ops=2; charged as 3 sequential logic steps; invocations UNVERIFIED (~191 implied)

**This prediction rests on an unverified declaration.** Gate 3 measures everything else it uses — the software cost from the profile, the instruction's work from its own C model — but not this:
  - invocations=48 is the designer's declaration and RED cannot verify it: one call of multiply executes 9,546 host instructions against this model's 50, which is consistent with roughly 191 invocations, not 48. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
```
