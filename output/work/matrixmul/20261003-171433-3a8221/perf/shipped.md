# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **7.79x** over 99.5% of profiled cycles.

The extension meets the target.

```
covers 99.5% of profiled cycles; predicted whole-application speedup 7.79x
  fgemm10.s: 2230 cyc (400 beats, 1000 MAC, 2.50 MAC/word, x30,000 per call) + 7740 marshalling vs 80207 cyc of software = 8.04x
```
