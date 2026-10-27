# Security review (Gate 4)

Checks run: encoding.collision, encoding.fields, encoding.opcode, latency.interrupt_bound, memory.bounds, memory.input_mutation, memory.output_residue, model.forbidden_call, model.impure, model.includes, timing.data_dependent, timing.secret_branch, timing.taint_scan
Vectors per model: 512

**Checks that could NOT run** (not the same as passing):
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection

**No mechanically confirmed blocking findings** — the gate passes.

## [MAJOR] `p256.xword` — agent.residue
*the security reviewer's reading — advisory*

**Finding.** A later unprivileged caller on the same hart can read the previous caller's secret multiplicand or accumulator by selecting its A/C limb with xword whenever the prior operation yields or exits before lda, stp, or a complete 16-pop drain clears that state.

```
The shared static xp256_a/xp256_c arrays persist across instruction calls; xword selects and returns an input word while updating its selected state limb, and the prose explicitly says xword can expose all state for context save/clear/restore. lda/stp clear state only at their specified sequence points, and a single instruction is non-interruptible but the multi-instruction sequence can be preempted between instructions. Probes are single-instruction, reset-state runs and cannot establish a cross-call read, so this remains advisory.
```
**Required change.** Make A/C state inaccessible to user-level instructions or tag and clear it on privilege/context changes; alternatively require the privileged context-switch path to save and zero the entire state before scheduling another caller, including interrupted sequences.
