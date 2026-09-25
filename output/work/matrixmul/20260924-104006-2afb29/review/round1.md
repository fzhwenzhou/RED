# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 4 blocking, 2 major, 1 minor

## [BLOCKING] `fdot2_custom` — benefit+callability
**Finding.** The instruction performs floating-point arithmetic on an integer workload, which will corrupt the results by reinterpreting integer bit patterns as IEEE-754 floats.
**Evidence.** The software baseline takes ~17.6 cycles per MAC (1692 cycles / 96 MACs replaced), confirming it uses integer arithmetic (soft-float operations would require hundreds of cycles per MAC). Furthermore, the application's `main` function initializes matrix elements with integer literals (e.g., `setElement(1, 1, 2, &A)`). Reinterpreting these integers as floats will treat small integer values as subnormals, which the instruction explicitly flushes to `0.0f` (`if (is_subnormal(a0)) a0 = 0.0f;`), silently zeroing out the entire matrix multiplication.
**Suggested fix.** Redesign the instruction to perform 32-bit integer multiply-accumulate operations to correctly match the workload's data types.

## [BLOCKING] `fdot2_custom` — callability
**Finding.** The 2-element dot product requires fetching two elements per iteration, which will cause out-of-bounds reads for matrices with an odd number of columns since the original source lacks a scalar remainder loop.
**Evidence.** The application's inner loop (`for (int k = 1; k <= matrixA->columns; k++)`) steps by 1. To marshal the operands for this 2-element instruction, the caller must fetch `A[k]` and `A[k+1]`. If `matrixA->columns` is odd, fetching `k+1` on the final iteration will read past the row bounds, pulling garbage data that will be multiplied and accumulated into the result.
**Suggested fix.** Reduce the instruction to a 1-element integer multiply-accumulate (which naturally aligns with the original loop's single-element step), or ensure the software environment dynamically pads matrix dimensions to even multiples.

## [BLOCKING] `fdot2_custom` — legality
**Finding.** The instruction incorrectly performs floating-point arithmetic for a workload that operates on integer data, which will cause integer matrix elements to be wrongly evaluated as subnormal floats and flushed to zero.
**Evidence.** The provided workload code initializes the matrix with integer literals (e.g., setElement(1, 1, 2, &A)) and uses integer validation (int z = x1 + x2;). The C model and pseudocode treat these bits as IEEE-754 floats and explicitly flush subnormals (if (is_subnormal(a0)) a0 = 0.0f;), meaning an integer value of 2 (0x00000002) will evaluate to 0.0, resulting in the application computing zero for all operations.
**Suggested fix.** Redesign the instruction semantics, C model, and pseudocode to perform a 32-bit integer dot product rather than floating-point.

## [BLOCKING] `fdot2_custom` — legality
**Finding.** Implementing custom floating-point multipliers and adders in hardware represents an extreme and unviable area increase for the minimally sized PicoRV32 core.
**Evidence.** The instruction's arithmetic specifies two single-precision floating-point multiplications and additions. The target PicoRV32 is explicitly 'optimized for minimum area', possessing a tiny footprint (~1024 flops) and relying solely on a sequential integer multiplier. Synthesizing custom IEEE-754 FP datapaths in the coprocessor would require thousands of logic cells, vastly exceeding and breaking the core's area constraints.
**Suggested fix.** Replace the floating-point arithmetic with 32-bit integer arithmetic to match the core's minimal area profile and enable alignment with existing integer hardware paths.

## [MAJOR] `fdot2_custom` — implementability
**Finding.** Adding hardware for single-precision floating-point multiplication and addition will consume thousands of LUTs, likely dwarfing the minimal-area PicoRV32 core.
**Evidence.** The instruction computes two floating-point MAC operations (`C + A0*B0 + A1*B1`). PicoRV32 is optimized for minimal area (~1000 LUTs) and explicitly lacks an FPU; adding even a sequenced floating-point MAC unit represents a massive area increase for this specific CPU.
**Suggested fix.** Reduce the instruction to a 1-element dot product (`words: 3`) to minimize the required floating-point combinational logic, or evaluate if the 6.26x speedup genuinely justifies more than doubling the core's area.

## [MAJOR] `fdot2_custom` — benefit
**Finding.** The declared invocations count is heavily overstated, distorting the performance projection.
**Evidence.** The spec claims `invocations: 48` per call, but the workload multiplies 2x2 matrices (8 MACs total). A 2-element dot product replacing the innermost `k` loop computes one full output element per execution, so it would be invoked exactly 4 times per `multiply::hot` call to produce the 4 output elements.
**Suggested fix.** Change `invocations` to 4 to match the 2x2 matrix dimensions.

## [MINOR] `fdot2_custom` — implementability
**Finding.** The instruction's latency is dominated by moving its operands over the memory bus rather than by its arithmetic.
**Evidence.** Moving 5 words in and 5 words out takes 48 cycles (28 issue + 20 bus beats), which exceeds the 40 cycles the performance gate charged for the arithmetic itself. Additionally, 8 of those bus cycles are entirely wasted writing padding zeros to `out[1..4]` due to the symmetric ABI.
**Suggested fix.** Tolerate the operand-dominated latency as an inherent cost of the RED ABI, or use a wider block to pack more MAC operations per invocation, amortizing both the fixed issue overhead and the output block waste.
