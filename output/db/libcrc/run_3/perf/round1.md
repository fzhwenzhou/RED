# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **23.06x** over 100.0% of profiled cycles.

The extension meets the target.

```
covers 100.0% of profiled cycles; predicted whole-application speedup 23.06x
  crc64_ecma_step: 71 cyc (6 beats, 0 MAC, 0.00 MAC/word, x1,024 per call) + 77 marshalling vs 2723 cyc of software = 18.35x — its C model executes 166 instructions, of which 166 are not accounted for by mac_ops=0; charged as 23 sequential logic steps
  crc32_step: 102 cyc (6 beats, 0 MAC, 0.00 MAC/word, x512 per call) + 77 marshalling vs 4901 cyc of software = 27.35x — its C model executes 388 instructions, of which 388 are not accounted for by mac_ops=0; charged as 54 sequential logic steps
  crc16_step: 102 cyc (6 beats, 0 MAC, 0.00 MAC/word, x512 per call) + 77 marshalling vs 4901 cyc of software = 27.39x — its C model executes 386 instructions, of which 386 are not accounted for by mac_ops=0; charged as 54 sequential logic steps
```
