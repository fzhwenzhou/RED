# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **4.62x** over 99.6% of profiled cycles.

The extension meets the target.

```
covers 99.6% of profiled cycles; predicted whole-application speedup 4.62x
  xqpack2.s: 38 cyc (0 beats, 0 MAC, 0.00 MAC/word, x20,000,000 per call) vs 802 cyc of software = 21.04x — its C model executes 73 instructions, of which 73 are not accounted for by mac_ops=0; charged as 10 sequential logic steps
  xqdot4: 32 cyc (0 beats, 4 MAC, 1.00 MAC/word, x60,000,000 per call) vs 267 cyc of software = 8.35x
  xqto.s: 37 cyc (0 beats, 0 MAC, 0.00 MAC/word, x20,000,000 per call) vs 802 cyc of software = 21.83x — its C model executes 63 instructions, of which 63 are not accounted for by mac_ops=0; charged as 9 sequential logic steps
```
