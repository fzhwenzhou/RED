# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **6.26x** over 96.6% of profiled cycles.

The extension meets the target.

```
covers 96.6% of profiled cycles; predicted whole-application speedup 6.26x
  fdot2_custom: 92 cyc (10 beats, 2 MAC, 0.20 MAC/word, x48 per call) + 129 marshalling vs 1692 cyc of software = 7.67x — its C model executes 227 instructions, of which 198 are not accounted for by mac_ops=2; charged as 40 sequential logic steps
```
