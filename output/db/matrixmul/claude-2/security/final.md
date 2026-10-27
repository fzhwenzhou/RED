# Security review (Gate 4)

Checks run: encoding.collision, encoding.fields, encoding.opcode, latency.interrupt_bound, memory.bounds, memory.input_mutation, memory.output_residue, model.forbidden_call, model.impure, model.includes, timing.data_dependent, timing.secret_branch, timing.taint_scan
Vectors per model: 512

**Checks that could NOT run** (not the same as passing):
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection

**No mechanically confirmed blocking findings** — the gate passes.

## [MAJOR] `xfop.lo, xfop.hi` — agent.side_channel
*the security reviewer's reading — advisory*

**Finding.** The fixed-latency claim is asserted for a non-normative sequential binary32 datapath that the mechanical timing checks never see, so a data-dependent normalise, round or early-out in the hardware would leak operand values (keys and nonces on these kernels).

```
Spec 'Timing: a fixed latency ... No early-out' for xfop.lo and xfop.hi, but rationale says 'Non-normative datapath: ... radix-4 sequential 24x24 significand multiplier (12 steps) ... about 16-20 cycles per multiply-add', and xfop.hi runs three of them back to back. timing.data_dependent and timing.secret_branch only inspect the C models, which have no cycle count; timing.secret_branch could not run (valgrind 'function redirection' fatal error, three times). Probes of zero, subnormal, Inf, NaN and tie operands returned clean values, but a probe returns no timing.
```
**Required change.** Make the latency a tested property of the RTL, not a claim: add an RTL testbench that issues xfop.lo and xfop.hi over zero, min/max subnormal, Inf, NaN, tie and normal operand sets and asserts identical cycle counts, and gate the spec on it. Also fix the valgrind redirection failure so timing.secret_branch actually runs.

## [MINOR] `xfop.lo, xfop.rd` — agent.residue
*the security reviewer's reading — advisory*

**Finding.** xfop.lo leaves the raw operands a0 and b0 in LA and LB, and the partial sums in FACC, where any code can read them with xfop.rd sel 4, 5 or 0..3, since the instruction has no privilege check. Clearing depends only on the caller's software scrub.

```
xfop.lo pseudocode 'FLAT[0]' = a0 // LA, raw bits' and 'FLAT[1]' = b0'. xfop.rd: 'sel 4: x[rd] = LA, then LA = 0' with no privilege condition. The register_files context says scrub 'on entry and exit' and 'on any context switch' is by software convention. Probes cannot exercise cross-caller state, since each probe runs from a fresh model.
```
**Required change.** Have xfop.hi clear FLAT after its last use, since every hi follows a lo in the kernel schedule, so the raw operand copy does not outlive the k step. Keep the FACC partial sums scrubbed by software, and state in the spec that no privilege separation exists so the residue rule is a software contract.

## [MINOR] `xfop.lo, xfop.hi` — agent.residue
*the security reviewer's reading — advisory*

**Finding.** The no-trap window claim is false on this build: loads inside a live block can trap on a misaligned address, which leaves FACC and FLAT live in the trap handler without any scrub.

```
Spec: 'Nothing on this core can come between them (no interrupts; the loads and address arithmetic in the loop cannot trap)'. The build has CATCH_MISALIGN=1 and CATCH_ILLINSN=1 (CoreProfile). FACC and FLAT are live from the first xfop.lo until the last xfop.rd drain, and the loads of the next k step run inside that window. Software treats state as clobbered across a trap, but the spec does not require the handler to scrub.
```
**Required change.** Correct the spec text to say loads can trap on this core, and either require the trap handler to issue xfop.rd x0, x0, 7 before running any other code, or document that a trap mid-block discards the block and the handler must scrub.
