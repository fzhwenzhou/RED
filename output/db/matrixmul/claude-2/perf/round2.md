# Performance gate

Target: 1.32x whole-application speedup.
Predicted: **1.98x** over 68.3% of profiled cycles.

The extension meets the target.

```
covers 68.3% of profiled cycles; predicted whole-application speedup 1.98x
  xfop.lo: 31 cyc (0 beats, 1 MAC, 0.25 MAC/word, x250 per call) vs 238 cyc of software = 7.67x
  xfop.hi: 34 cyc (0 beats, 3 MAC, 0.75 MAC/word, x167 per call) vs 356 cyc of software = 10.40x
  xfop.rd: 43 cyc (0 beats, 0 MAC, 0.00 MAC/word, x70 per call) vs 849 cyc of software = 19.93x — its C model executes 91 instructions, of which 91 are not accounted for by mac_ops=0; charged as 13 sequential logic steps
```
