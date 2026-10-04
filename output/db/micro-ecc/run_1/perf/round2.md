# Performance gate

Target: 1.32x whole-application speedup.
Predicted: **8.72x** over 97.2% of profiled cycles.

The extension meets the target.

```
covers 97.2% of profiled cycles; predicted whole-application speedup 8.72x
  secp256r1.mult: 297 cyc (32 beats, 64 MAC, 2.00 MAC/word) + 413 marshalling vs 8748 cyc of software = 12.32x — its C model executes 1,723 instructions, of which 781 are not accounted for by mac_ops=64; charged as 108 sequential logic steps
  secp256r1.mmod: 641 cyc (32 beats, 0 MAC, 0.00 MAC/word, x2 per call) + 206 marshalling vs 34754 cyc of software = 41.03x — its C model executes 3,720 instructions, of which 3,720 are not accounted for by mac_ops=0; charged as 516 sequential logic steps
  secp256r1.modsub: 158 cyc (32 beats, 0 MAC, 0.00 MAC/word) + 619 marshalling vs 1204 cyc of software = 1.55x — its C model executes 241 instructions, of which 241 are not accounted for by mac_ops=0; charged as 33 sequential logic steps
```
