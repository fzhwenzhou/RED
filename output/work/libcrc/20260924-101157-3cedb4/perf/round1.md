# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **6.25x** over 99.9% of profiled cycles.

The extension meets the target.

```
covers 99.9% of profiled cycles; predicted whole-application speedup 6.25x
  crc64_ecma_step32b: 81 cyc (6 beats, 0 MAC, 0.00 MAC/word, x833 per call) + 77 marshalling vs 1022 cyc of software = 6.44x — its C model executes 166 instructions, of which 166 are not accounted for by mac_ops=0; charged as 33 sequential logic steps
  crcccitt_step32b: 87 cyc (4 beats, 0 MAC, 0.00 MAC/word, x833 per call) + 52 marshalling vs 920 cyc of software = 6.62x — its C model executes 227 instructions, of which 227 are not accounted for by mac_ops=0; charged as 45 sequential logic steps; invocations UNVERIFIED (~397 implied)
  crc32_step32b: 81 cyc (4 beats, 0 MAC, 0.00 MAC/word, x833 per call) + 52 marshalling vs 920 cyc of software = 6.92x — its C model executes 196 instructions, of which 196 are not accounted for by mac_ops=0; charged as 39 sequential logic steps
  crc16_step32b: 87 cyc (4 beats, 0 MAC, 0.00 MAC/word, x833 per call) + 52 marshalling vs 920 cyc of software = 6.62x — its C model executes 227 instructions, of which 227 are not accounted for by mac_ops=0; charged as 45 sequential logic steps; invocations UNVERIFIED (~397 implied)
  crcdnp_step32b: 87 cyc (4 beats, 0 MAC, 0.00 MAC/word, x833 per call) + 52 marshalling vs 920 cyc of software = 6.62x — its C model executes 227 instructions, of which 227 are not accounted for by mac_ops=0; charged as 45 sequential logic steps; invocations UNVERIFIED (~397 implied)
  crc8_step32b: 87 cyc (4 beats, 0 MAC, 0.00 MAC/word, x833 per call) + 52 marshalling vs 613 cyc of software = 4.41x — its C model executes 227 instructions, of which 227 are not accounted for by mac_ops=0; charged as 45 sequential logic steps; invocations UNVERIFIED (~264 implied)

**This prediction rests on an unverified declaration.** Gate 3 measures everything else it uses — the software cost from the profile, the instruction's work from its own C model — but not this:
  - invocations=833 is the designer's declaration and RED cannot verify it: one call of crc_ccitt_1d0f executes 90,013 host instructions against this model's 227, which is consistent with roughly 397 invocations, not 833. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
  - invocations=833 is the designer's declaration and RED cannot verify it: one call of crc_16 executes 90,013 host instructions against this model's 227, which is consistent with roughly 397 invocations, not 833. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
  - invocations=833 is the designer's declaration and RED cannot verify it: one call of crc_dnp executes 90,016 host instructions against this model's 227, which is consistent with roughly 397 invocations, not 833. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
  - invocations=833 is the designer's declaration and RED cannot verify it: one call of crc_8 executes 60,009 host instructions against this model's 227, which is consistent with roughly 264 invocations, not 833. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
```
