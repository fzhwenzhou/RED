# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **2.56x** over 99.2% of profiled cycles.

The extension meets the target.

```
covers 99.2% of profiled cycles; predicted whole-application speedup 2.56x
  fmac_custom: 61 cyc (6 beats, 1 MAC, 0.17 MAC/word, x191 per call) + 103 marshalling vs 425 cyc of software = 2.59x
```
