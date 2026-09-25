# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 3 blocking, 1 major, 1 minor

## [BLOCKING] `imac_custom` — callability
**Finding.** The instruction is a net performance loss because it forces the caller to pay memory marshalling and PCPI issue overheads for a single integer multiply-add, which the core's native RV32M extension already does natively in registers.
**Evidence.** The instruction processes a 3-word memory block costing 41 cycles (28 issue + 12 memory + 1 ops) plus >150 cycles of memory marshalling overhead, whereas native RV32M performs a `mul` and `add` entirely in registers in ~33 cycles with zero marshalling.
**Suggested fix.** Design an instruction that computes multiple MACs per invocation (e.g., accelerating a larger portion of the matrix multiplication) so the arithmetic heavily outweighs the fixed memory marshalling costs, or abandon the extension as native RV32M is already optimal for single MACs.

## [BLOCKING] `imac_custom` — benefit
**Finding.** The scalar instruction is slower than the software it replaces and incorrectly claims to replace the entire multiply kernel, resulting in an extension that cannot reach a useful speedup (ratio < 1.0).
**Evidence.** A single integer MAC takes 41 cycles in hardware (28 issue + 12 memory + 1 ops) plus ~30 cycles of marshalling, which is slower than the base core's native multiply and add (~33 cycles worst-case). Furthermore, because it only performs a scalar MAC, it replaces an inner statement rather than the whole `multiply::hot` kernel. The heavy software loop and `getElement` overhead remains, so true execution time would increase from 1692 cycles to ~1996 cycles.
**Suggested fix.** Redesign the instruction to perform a full 2x2 matrix multiplication in one invocation to replace the whole kernel. The fused version should set `words=8`, taking an 8-word input block (4 elements of A, 4 elements of B) and writing an 8-word output block (4 elements of C, padded with zeros). This executes 8 MACs in one call, amortizing the 28-cycle issue overhead and eliminating the software loops entirely.

## [BLOCKING] `imac_custom` — legality
**Finding.** The instruction duplicates the core's existing hardware multiplier to perform a single 32-bit scalar integer MAC, which violates core constraints and is strictly slower than using standard RV32IM instructions.
**Evidence.** The architectural constraints explicitly state 'functional units already present (do not duplicate): Multiplier'. Offloading a single 32-bit MAC (in[0] + in[1] * in[2]) to a memory-operand PCPI coprocessor incurs 28+ cycles of issue latency and 12 cycles of memory transfer overhead for an operation the core already natively computes in fewer cycles using MUL and ADD.
**Suggested fix.** Accelerate a larger block of work, such as a multi-element integer dot product (e.g., 4 to 8 elements) per invocation. This amortizes the PCPI issue latency and memory marshaling overhead, providing actual acceleration over scalar software loops without pointlessly duplicating the scalar multiplier.

## [MAJOR] `imac_custom` — implementability
**Finding.** The instruction duplicates the core's existing integer multiplier to perform a single 32-bit MAC operation.
**Evidence.** The core profile explicitly states it already has a multiplier (sequential or DSP) and forbids duplicating functional units; building a 32-bit multiplier in the PCPI coprocessor just for a single scalar MAC directly violates this constraint.
**Suggested fix.** Design an instruction that performs vector operations or unrolled arithmetic (e.g., computing a dot product of 4-8 elements at once) to justify the custom hardware, rather than reproducing the base ISA's scalar multiplication capability.

## [MINOR] `imac_custom` — implementability
**Finding.** The instruction's latency is heavily dominated by moving its operands over the memory bus rather than by its arithmetic.
**Evidence.** Transferring three 32-bit words in and out costs 12 cycles plus 28 cycles of fixed issue overhead (40 cycles total operand penalty), while the single scalar MAC operation provides just 1 MAC of work, yielding an extremely poor arithmetic intensity of 0.33 MACs per word moved.
**Suggested fix.** Increase the operand block size to process multiple elements per invocation (e.g., loading a small matrix tile or vector) to amortize the fixed 28-cycle issue overhead over multiple multiply-accumulate operations.
