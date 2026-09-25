# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 3 blocking, 4 major, 3 minor

## [BLOCKING] `fdot2_custom` — implementability
**Finding.** The instruction requires two single-precision floating-point multipliers and adders, which would dwarf the minimalist PicoRV32 core in area.
**Evidence.** The target core is an extremely small, area-optimized RV32I integer core without an FPU. Implementing two independent IEEE 754 float multiplications and additions in hardware requires large 24x24 multipliers, barrel shifters, and leading-zero counters, massively violating the constraint that the extension's area footprint must be kept extremely small.
**Suggested fix.** Redesign the extension to process a single floating-point MAC sequentially over multiple cycles to minimize datapath area, or operate on quantized integer data if the algorithm permits.

## [BLOCKING] `fdot2_custom` — callability
**Finding.** Implementing two 32-bit floating-point multipliers and two adders as combinational logic requires immense area, severely violating the strict footprint constraint of this minimalist core.
**Evidence.** The C model performs two parallel native-float multiplications and two additions per invocation. A single FP32 multiplier is roughly the size of the entire PicoRV32 core (~10k gates); building four FP units to compute this combinationally would massively multiply the core's area, which is explicitly forbidden by the constraint to avoid large area footprints.
**Suggested fix.** Redesign the instruction to perform a single floating-point MAC (words=3) to drastically reduce the arithmetic hardware required, which will still easily pay for itself by eliminating the software soft-float overhead.

## [BLOCKING] `fdot2_custom` — benefit
**Finding.** The instruction proposes dual parallel combinatorial floating-point MACs in an area-optimized integer core, massively exceeding area constraints and taking far more than the credited 2 cycles.
**Evidence.** The target is a minimal PicoRV32 without an FPU. The C model performs two IEEE-754 32-bit float multiplications and additions per invocation. The performance gate priced this at 2 cycles based on `mac_ops=2` (which assumes integer MACs), but building a 2-cycle dual FP-MAC datapath would dwarf the entire core's area, completely violating the "extremely small" footprint constraint.
**Suggested fix.** Redesign the instruction to use integer or fixed-point arithmetic if the workload allows it, or implement a multi-cycle sequential FPU datapath, though this may still violate the strict area constraints.

## [MAJOR] `fdot2_custom` — implementability
**Finding.** The declared number of invocations (48) significantly undercounts the true data volume processed per call.
**Evidence.** The performance gate reports that a single call to the multiply loop executes 9,546 host instructions. Since the software loop requires roughly 50 host instructions per iteration, this implies approximately 191 invocations per call, not 48.
**Suggested fix.** Update `invocations` to 191 to correctly reflect the true loop trip count and data volume in the workload profile.

## [MAJOR] `fdot2_custom` — benefit
**Finding.** The instruction replaces an inner statement rather than the whole kernel, requiring explicit loop unrolling, and gives a maximum application speedup of ~15.0x.
**Evidence.** Instruction cost is 50 cycles (28 + 2*(10) + 2). Multiplied by 48 invocations, this is 2400 cycles per call. Adding marshaling (50, or 2400 if correctly charged per invocation) gives 4800 cycles per call. Compared to a software cost of 81216 cycles (1692 * 48), the ratio is 16.9x. The instruction replaces an inner statement, so the fused version unrolls the innermost `k` loop by 2: `in[0]=A(i,k); in[1]=A(i,k+1); in[2]=B(k,j); in[3]=B(k+1,j); in[4]=C(i,j); fdot2_custom(in, out); C(i,j)=out[0];`. By Amdahl's law for 99.2% coverage, maximum speedup is bounded at ~15.0x.
**Suggested fix.** Update the specification to acknowledge this is an inner loop accelerator that relies on the host CPU for outer loops and address generation.

## [MAJOR] `fdot2_custom` — legality
**Finding.** NaN payloads are left undefined, which will cause cross-platform differential tests to fail based on the host architecture's native float implementation.
**Evidence.** For inputs like 0xFFFFFFFF (NaN), standard C operations like `a0 * b0` inherit the host CPU's NaN propagation rules (e.g., x86 vs RISC-V). The spec claims to discard exceptions but does not specify a canonical NaN output.
**Suggested fix.** Update the prose, pseudocode, and C model to explicitly canonicalize any NaN output to the RISC-V canonical NaN (0x7FC00000) after every floating-point operation.

## [MAJOR] `fdot2_custom` — legality
**Finding.** The pseudocode omits the flush-to-zero (FTZ) step on the inputs, contradicting the prose and C model.
**Evidence.** The prose states 'flushes any subnormal input values to zero' and the C model correctly applies `ftz_u32` to all elements of `in`, but the pseudocode simply does `a0 = in[0]` without `ftz`.
**Suggested fix.** Update the pseudocode to apply `ftz()` to all inputs before arithmetic, for example `a0 = ftz(in[0])`.

## [MINOR] `fdot2_custom` — callability
**Finding.** The declared marshal_words of 5 undercounts the true marshalling cost the caller must perform.
**Evidence.** Because the operands b0 and b1 are strided in matrixB, the caller cannot pass a direct pointer. It must write all 5 input operands (a0, a1, b0, b1, c) into a temporary input block, and then read the 1-word result from the output block, totaling 6 memory moves per invocation.
**Suggested fix.** Update marshal_words to 6.

## [MINOR] `fdot2_custom` — callability
**Finding.** The symmetric 5-word output block forces the caller to use a temporary output buffer and wastes memory bus bandwidth.
**Evidence.** Because RED's ABI uses the same 'words' size for both blocks, the instruction writes 5 words back to memory (padding out[1..4] with zeros). The caller cannot point rs2 directly to matrixC[i][j] without corrupting adjacent elements, forcing the use of a stack buffer and wasting 8 cycles of bus bandwidth per invocation on writing zeros.
**Suggested fix.** This is a consequence of the RED ABI, but reducing the instruction to a single MAC (words=3) would proportionally reduce both the wasted bandwidth and the size of the required stack buffer.

## [MINOR] `fdot2_custom` — benefit
**Finding.** The instruction writes 4 words of unused zeros to memory on every invocation, wasting memory bandwidth.
**Evidence.** RED's ABI forces the output block size to match the input (`words=5`). The C model zeroes `out[1]` through `out[4]`, writing 16 bytes of useless data per invocation and wasting 8 cycles on the coprocessor bus each time.
**Suggested fix.** Use a smaller block size by accumulating the dot product internally over multiple invocations and writing the final sum to memory only at the end of the row, or pad the ABI efficiently.
