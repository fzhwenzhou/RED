# Security review (Gate 4)

Checks run: encoding.collision, encoding.fields, encoding.opcode, latency.interrupt_bound, memory.bounds, memory.input_mutation, memory.output_residue, model.forbidden_call, model.impure, model.includes, timing.data_dependent, timing.secret_branch, timing.taint_scan
Vectors per model: 512

**2 mechanically confirmed blocking finding(s)** — these fail the gate.

## [BLOCKING] `fmac_custom` — timing.data_dependent
*a tool observed this*

**Finding.** the model executes 127 instructions for one operand value and 121 for another (4.7% spread), so the instruction's latency leaks its operands through timing

```
callgrind, collection toggled inside fmac_custom_model:
         127 Ir for in = ffffffffffffffffffffffff...
         121 Ir for in = 000000000000000000000000...
```
**Required change.** make the model branch-free and fixed-trip: compute every branch's result and select with a mask (`m = -(uint32_t)cond; r = (x & m) | (y & ~m)`), and give every loop a trip count that depends only on `words`

## [BLOCKING] `fmac_custom` — timing.secret_branch
*a tool observed this*

**Finding.** with the operand block marked as secret, the model branches on it or uses it to compute an address — a timing / cache side channel on the very values the instruction exists to protect

```
==8206== Conditional jump or move depends on uninitialised value(s)
==8206==    at 0x10915F: canon_nan (fmac_custom_ctgrind.c:30)
==8206==    by 0x1091B1: fmac_custom_model (fmac_custom_ctgrind.c:45)
==8206==    by 0x109291: main (fmac_custom_ctgrind.c:71)
==8206== 
==8206== Conditional jump or move depends on uninitialised value(s)
==8206==    at 0x10915F: canon_nan (fmac_custom_ctgrind.c:30)
==8206==    by 0x1091C7: fmac_custom_model (fmac_custom_ctgrind.c:49)
==8206==    by 0x109291: main (fmac_custom_ctgrind.c:71)
==8206==
```
**Required change.** replace the data-dependent branch or table index with masked arithmetic that touches the same addresses for every input
