# Performance gate

Target: 2.00x whole-application speedup.
Predicted: **5.08x** over 90.5% of profiled cycles.

The extension meets the target.

```
covers 90.5% of profiled cycles; predicted whole-application speedup 5.08x
  secp256r1.mult: 329 cyc (32 beats, 64 MAC, 2.00 MAC/word) + 413 marshalling vs 11225 cyc of software = 15.13x
  secp256r1.mmod: 1463 cyc (32 beats, 0 MAC, 0.00 MAC/word) + 206 marshalling vs 10247 cyc of software = 6.14x — its C model executes 6,045 instructions, of which 6,045 are not accounted for by mac_ops=0; charged as 1209 sequential logic steps; invocations UNVERIFIED (~0 implied)

**This prediction rests on an unverified declaration.** Gate 3 measures everything else it uses — the software cost from the profile, the instruction's work from its own C model — but not this:
  - invocations=1 is the designer's declaration and RED cannot verify it: one call of vli_mmod_fast_secp256r1 executes 1,204 host instructions against this model's 6,045, which is consistent with roughly 0 invocations, not 1. That ratio is itself only indicative — the two sides are different implementations — but the gap is too large to ignore. Check it against the data volume: how many bytes does one call process, and how many does one invocation?
```
