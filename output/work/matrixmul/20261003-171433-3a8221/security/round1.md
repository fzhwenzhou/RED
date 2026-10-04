# Security review (Gate 4)

Checks run: encoding.collision, encoding.fields, encoding.opcode, latency.interrupt_bound, memory.bounds, memory.input_mutation, memory.output_residue, model.forbidden_call, model.impure, model.includes, timing.data_dependent, timing.secret_branch, timing.taint_scan
Vectors per model: 512

**Checks that could NOT run** (not the same as passing):
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection

**No mechanically confirmed blocking findings** — the gate passes.

## [MINOR] `fgemm10.s` — agent.bounds
*the security reviewer's reading — advisory*

**Finding.** The spec says the 200-word input and output blocks 'must not overlap' and must be word-aligned, but does not say what the hardware does when they overlap, are misaligned, or when rs1+796 or rs2+796 wraps past 0xFFFFFFFF. The streaming bus-master implementation it describes (re-reading A row i and B column j for every element while writing out[10*i+j]) would then read back its own partial results, so the output depends on the implementation and cannot be reproduced by the C model.

```
Semantics: 'rs1 holds the word-aligned address of a 200-word input block ... The two blocks must not overlap.' No trap or defined result is given for a violation. The hardware section says it 'reads A row i (10 words) and B column j (10 words, stride 40 bytes)' per element with no 200-word buffer, so a write to out[] that aliases in[] changes later reads. Probes of IEEE corner cases (subnormal accumulation 0x00400001, sNaN and inf*0 giving 0x7FC00000, -0 chain giving +0, overflow to inf, inf+(-inf) giving 0x7FC00000, underflow to 0, ulp*FLT_MAX=0x34ffffff) all came back bit-correct, so the arithmetic is not the problem. The only undefined behaviour left is at the interface.
```
**Required change.** Define the violation. Either (a) have the accelerator check alignment (rs1[1:0]|rs2[1:0] != 0), 32-bit wrap of [rs1, rs1+800) and [rs2, rs2+800), and overlap of the two ranges, and on any of these raise no pcpi_wait so the instruction traps as illegal (the existing CATCH_ILLINSN path), or (b) state in the semantics that out[] is unspecified on violation, and add the same three checks to the caller's fast-path guard in multiply().
