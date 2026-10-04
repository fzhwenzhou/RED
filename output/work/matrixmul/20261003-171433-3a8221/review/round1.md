# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 0 blocking, 12 major, 10 minor

## [MAJOR] `fgemm10.s` — implementability
**Finding.** The hardware described in `semantics` moves about 5.5x more words than the 400 beats the gate charged it for. The 400-beat cost would require a 100-word B buffer, which the spec explicitly rules out.
**Evidence.** The semantics say the unit reads, for each element, A row i (10 words) and B column j (10 words, 40-byte stride), with 'no 200-word buffer'. That is 100 x 20 = 2000 reads plus 200 writes (out[0..99] and the 100 zero words), so 2200 words x 2 = 4400 cycles of traffic against the gate's 800. The allowed row-buffer variant reads A 100 + B 1000 + writes 200 = 1300 words, or 2600 cycles, and adds 10 words (320 flops). To reach 400 beats, every input word may be read only once. Each B[k][j] is used by all 10 values of i, so B must be held entirely: 100 words, 3200 flops, which is about 3x the core's 992-bit register file and well past the 32-word 'large' threshold. A[i][*] must also be cached (320 flops). Realistic latency is therefore 28 + 4400 = about 4.4k cycles (streaming, 96 flops of operan
**Suggested fix.** Pick one microarchitecture and state its real cost. Recommended: the row-buffer variant, with 10-word A cache, 1 accumulator and 1 B word, about 420 flops total. Its 1300 bus words (2600 cycles) let 2-cycle-per-word fetch hide a multi-cycle FP unit. Put a note in the spec that latency is about 2.7-3.7k cycles. If the 400-beat figure is to be kept, declare the 3200+320-flop A/B buffers and own that area.

## [MAJOR] `fgemm10.s` — implementability
**Finding.** `mac_ops`=1000 prices each binary32 multiply followed by a separately rounded add as 1 cycle. In hardware, the loop-carried accumulator makes each element a serial chain of 10 mul-round-add-round steps, and none of them is 1 cycle at this core's clock.
**Evidence.** The c_model computes `p = a*b; acc = acc + p;` as two rounded operations, with acc_{k+1} depending on acc_k. A realistic binary32 multiply needs about 1-2 cycles. A full IEEE add with subnormals and RNE (align, add, LZC normalize, round) needs about 2-3 cycles on a PicoRV32-class timing budget. With a single datapath and no interleaving of the 100 independent accumulators, the serial cost is about 4-5 cycles per MAC, so 4000-5000 cycles rather than 1000. With full A/B buffers (the only way to get 400 beats), this becomes the bottleneck: 28 + 800 + about 4500 = about 5.3k cycles, or about 6.3x per call instead of 8.04x. It does not change the verdict (software is 80207 cycles), but the declared figure is about 4-5x too low for a 1-MAC-per-cycle claim the spec never justifies.
**Suggested fix.** Declare the pipeline depth. Either set mac_ops to about 4000-5000 (serial mul+add latency x 1000), or specify that the unit interleaves at least L independent (i,j) accumulators (L = adder latency, for example 4 accumulators = 128 flops) to sustain 1 MAC per cycle. With the streaming or row-buffer fetch, simply note that the 4-cycle-per-MAC operand fetch hides the FP latency.

## [MAJOR] `fgemm10.s` — implementability
**Finding.** The 24x24 significand multiplier is a second multiplier array next to fast_mul's 33x33 array. The context defines that as 'large' on this core. The justification that fast_mul is 'unreachable from PCPI' does not hold, because the CPU is stalled for the whole instruction.
**Evidence.** fast_mul is itself a PCPI slave inside the same core. While fgemm10.s holds pcpi_wait, no MUL can issue, so the 33x33 array sits idle for the entire 2-5k-cycle instruction. A 24x24 array is about 53% of a 33x33 array's partial products (576 vs 1089), so it is the largest combinational block in the accelerator. On top of that, the bus-master coprocessor already costs +74% of core area.
**Suggested fix.** Mux the significand operands into fast_mul's 33x33 array while fgemm10.s is busy (zero-extend 24-bit significands, take the 48-bit product from the 66-bit result). This needs one operand mux and no second array. Alternatively, keep the dedicated multiplier and state its area explicitly as part of the cost.

## [MAJOR] `fgemm10.s` — callability
**Finding.** The fast-path guard tests the wrong dimensions. It also changes C's dimensions, which the original multiply() may never do, so the fast path can give a different C than the software loop.
**Evidence.** The original nest takes its trip counts from matrixC->rows, matrixC->columns and matrixA->columns. It addresses A with A->columns and B with B->columns. A->rows and B->rows never enter the loop. The spec's guard is 'A is 10x10, B is 10x10, and C does not alias A or B', and then it 'sets C's rows and columns to 10'. Take A and B as 10x10 and C as 5x5 on entry. The software computes a 5x5 product, laid out with stride 5 in C->elements. The fast path writes a 10x10 product with stride 10 and changes C's dimensions. This is a mismatch the caller can actually hit, unless multiply() itself already sets C's dimensions before the nest. The summarised source does not show that it does.
**Suggested fix.** Make the guard exactly the loop's own parameters: C->rows == 10, C->columns == 10, A->columns == 10 and B->columns == 10, with no aliasing. Do not write C->rows or C->columns on the fast path unless the original multiply() does so before the nest. If it does, say so and cite that line.

## [MAJOR] `fgemm10.s` — callability
**Finding.** The 400-beat cost model the gate used does not match the datapath the spec describes, which re-reads B (and, without a row buffer, A) for every output element.
**Evidence.** The semantics say that for each element it 'reads A row i (10 words) and B column j (10 words, stride 40 bytes)' and keeps 'no 200-word buffer'. That is 100 x 20 = 2,000 reads, plus 100 result writes and 100 zero writes: 2,200 beats, about 4,400 cycles. The gate charged 400 beats (800 cycles). With the allowed A-row buffer it is still 100 + 1,000 + 200 = 1,300 beats, about 2,600 cycles. The '1 x mac_ops' term also assumes one cycle per separately rounded FMUL+FADD with full subnormal support, which is optimistic for a PicoRV32-class design. At 3 cycles per MAC, the 'no buffer' instruction costs about 28 + 4,400 + 3,000 ≈ 7,430 cycles. With 7,740 cycles of marshalling, that gives roughly 80,207 / 15,170 ≈ 5.3x. This changes the size of the gain, not the verdict.
**Suggested fix.** Pick one microarchitecture and state its cost. Either (a) buffer all of B (100 words, 3,200 bits, which counts as 'large' on this core and should be declared as area), so traffic really is 200 reads and 100 writes; or (b) keep the streaming design and declare the true beat count and MAC latency so the gate prices 1,300 to 2,200 beats and the real cycles per MAC.

## [MAJOR] `fgemm10.s` — callability
**Finding.** The instruction only works for 10x10x10. The application's own types allow other shapes (8-bit rows and columns, up to MAX_ELEMENTS=100), and every one of them gets nothing from a full binary32 multiply-add unit.
**Evidence.** The hot-loop summary says 'Exact trip count unknown from sources (harness-driven)'. The 10x10 assumption comes only from the profiled run. Shapes such as 5x20 times 20x5, or 4x25 times 25x4, fit in float[100] and take the software fallback at about 80+ cycles per MAC. The FP datapath (24x24 significand multiplier plus align, normalise and round) is a large area cost on a core of 1-2k flops, and it pays off only on one exact shape.
**Suggested fix.** Put the dimensions into in[0] as a header word (M, K, N as 8-bit fields, with M*K <= 100, K*N <= 100 and M*N <= 100), followed by A and then B. Loop the existing FSM over those bounds. The hardware cost is three counters, and every shape the Matrix struct can hold is covered. At minimum, confirm from the harness source that only 10x10 is ever multiplied, and record that in the spec.

## [MAJOR] `fgemm10.s` — benefit
**Finding.** The hardware described in the semantics moves about 5.5x more bus words than the 400 beats the cost model charges for, so the 8.04x per-call figure is optimistic.
**Evidence.** The formula charges 2 x words = 400 beats (200 in, 200 out), which is 28 + 800 + 1000 = 1828 cycles. The semantics instead say the unit keeps 'only the accumulator plus one operand pair as state (no 200-word buffer)' and per element 'reads A row i (10 words) and B column j (10 words)'. That is 100 elements x 20 = 2000 reads plus 200 writes = 2200 beats = 4400 cycles. The allowed row-buffer variant still reads 100 (A) + 1000 (B) + 200 writes = 1300 beats = 2600 cycles. Repricing the streaming design gives 28 + 4400 + 1402 (the gate's measured compute) + 7740 marshalling = 13,570 cycles against 80,207 for software, or 5.9x per call. That still pays, but the 400-beat figure only holds if the unit buffers all of B, which is 100 words (3,200 bits). By the core's own threshold of 32 words that i
**Suggested fix.** Pick one implementation and price it honestly. Either (a) state the streaming or row-buffer variant and accept a cost of about 2,600 to 4,400 bus cycles, which still gives about 6 to 7x per call, or (b) state a 100-word B buffer, read every input word exactly once, and declare its 3,200 bits of state as the area cost. Do not describe the cheap-area design while claiming the cheap-traffic cycle count.

## [MAJOR] `fgemm10.s` — benefit
**Finding.** Pricing a separately rounded binary32 multiply and a separately rounded binary32 add at 1 cycle per MAC is not credible for a PicoRV32-class datapath.
**Evidence.** The cost formula charges 1 cycle per mac_op. Each MAC here is a 24x24 significand multiply, an RNE round, an align/add/normalize, a second RNE round, and full subnormal support with no early exit. The two roundings are back to back and cannot be fused, so a single-cycle path at this core's clock is unlikely. A 3-to-4-cycle iterative unit is realistic, which adds 2,000 to 3,000 cycles per call. With the realistic streaming traffic from the previous finding: 28 + 4400 + 4000 + 7740 = 16,168 cycles against 80,207, or 5.0x per call. Amdahl at 99.5% coverage gives 1 / (0.005 + 0.995/5.0), about 4.9x for the whole application. That is still a strong win, but it is not 7.79x.
**Suggested fix.** State the MAC latency in cycles (for example a 4-cycle mul-round-add-round pipeline) and set mac_ops to account for it, or describe the pipelining that sustains 1 MAC per cycle given the 2-cycle-per-word operand fetch. Report the expected whole-application speedup as roughly 5x, not 7.8x.

## [MAJOR] `fgemm10.s` — benefit
**Finding.** The instruction only fires for exactly 10x10 times 10x10, but the hot-loop report says the trip count is unknown and harness-driven, so the 99.5% coverage depends on an unverified dimension.
**Evidence.** The hot-loop report says 'Dimensions bounded by 100 elements... Exact trip count unknown from sources (harness-driven).' The spec says 'the profiled N=10 case' and the caller 'uses the instruction only when A is 10x10, B is 10x10', and otherwise runs software. If the harness also drives other shapes (any r x c with r*c <= 100), those calls get 1.0x. Amdahl then caps the gain at 1 / (1 - f_10x10) instead of the 99.5% figure.
**Suggested fix.** Cite the harness source or profile evidence showing every multiply() call is 10x10x10. If it is not, either add a shape-generic variant (pass rows, columns and inner dimension in a header word of the input block, with a fixed upper bound of 10) or report the 10x10 fraction of calls as the real coverage.

## [MAJOR] `fgemm10.s` — legality
**Finding.** The hardware the semantics describe moves about 5x more bus words than the 400 beats the gate priced it at, and the only way to reach 400 beats needs far more operand buffer state than the spec declares.
**Evidence.** The semantics say: 'per element it reads A row i (10 words) and B column j (10 words, stride 40 bytes) ... (no 200-word buffer)'. That is 100 elements x 20 = 2,000 reads plus 200 writes, so 2,200 words x 2 = 4,400 bus cycles, against the modelled 2x(2x200) = 800. The allowed row-buffer variant still reads 100 + 1,000 = 1,100 words, so (1,100 + 200) x 2 = 2,600 cycles. Reading each word only once, as the gate assumed, means buffering all of B: 100 words = 3,200 bits, about 3x the register file. That is well past the core's 'large' threshold of 32 words. The serial acc chain also makes 1 cycle per MAC optimistic unless about 10 accumulators are interleaved. The verdict does not flip: about 28 + 4,400 + 1,000 + 7,740 marshal = about 13.2k cycles, against 80,207 for software, is still about 6x
**Suggested fix.** Choose one microarchitecture and declare its real cost. (a) Buffer B once (100 words) and stream A, keeping 10 accumulators for row i, so traffic is 200 reads + 100 writes. State this 3,200-bit buffer as the area cost. Or (b) keep the streaming design and update the cost model to 2,200 words moved. Do not leave both described as 'identical' when their cost and area differ by this much.

## [MAJOR] `fgemm10.s` — legality
**Finding.** The spec does not show that the profiled multiplies are actually 10x10 by 10x10. It only shows that 10x10 is the largest square size that fits the 100-element array. If the harness uses any other shape, every call takes the software fallback and the instruction gives nothing.
**Evidence.** The hot-loop analysis says: 'Dimensions bounded by 100 elements ... Exact trip count unknown from sources (harness-driven).' The description asserts 'the profiled N=10 case' and invocations = 30,000 but cites no source. Shapes such as 4x25 by 25x4, or 5x5, also fit MAX_ELEMENTS, and the fast-path test 'A is 10x10, B is 10x10' rejects all of them.
**Suggested fix.** Cite the harness source or profile evidence that the matrices are 10x10 (for example the k trip count per (i,j) from callgrind: 1000 inner iterations per multiply). If the shape varies, make the dimensions operands, for example rows/cols packed into a header word of the input block, with trip counts that are fixed per call.

## [MAJOR] `fgemm10.s` — legality
**Finding.** The instruction is not total: the result for overlapping input and output blocks is left undefined ('The two blocks must not overlap'). The streaming implementation it describes would give results that depend on the order of reads and writes.
**Evidence.** The semantics write out[10*i+j] while A rows and B columns are still being read from in[]. If rs2 lies inside [rs1, rs1+800), later elements read words that were already overwritten. The prose names no defined result, trap, or required ordering for this case.
**Suggested fix.** Define the behaviour for every rs1 and rs2. Either say that all 200 input words are logically read before any output word is written, so results are as if the copy were taken first (the implementation then has to buffer or detect the overlap), or say that overlap gives an unspecified but deterministic value with no other side effects.

## [MINOR] `fgemm10.s` — implementability
**Finding.** The ABI requires out[100..199] to be zero-filled. That costs 200 cycles of bus writes per call, and the 200-word output block stops the caller from pointing rs2 directly at C->elements[100], which forces the 100-word copy-back in marshal_words.
**Evidence.** The semantics say 'out[100] through out[199] are each written with 0x00000000'. That is 100 words x 2 = 200 cycles of no use. C->elements is float[100] followed by the rows/columns bytes, so a 200-word write through rs2 = C->elements would overwrite the struct. The caller therefore needs scratch space plus a 100-word copy, about 1/3 of the 7740 marshalling cycles. Marshalling (7740) already costs more than the instruction itself.
**Suggested fix.** Within the fixed ABI this cannot be removed, so document it as a cost. The caller should keep one static 200-word scratch output buffer (not stack-allocated per call) and copy out[0..99]. Note in the spec that marshalling, not arithmetic, dominates the call.

## [MINOR] `fgemm10.s` — implementability
**Finding.** rd returns out[0] (C[0][0]), but RED's fixed ABI says rd <- 0.
**Evidence.** The ABI says 'rd <- 0'. The semantics say 'rd receives out[0] (C[0][0]) with pcpi_wr=1'. The pseudocode ends 'rd = out[0]'. That conflicts with the convention every other RED instruction follows, and the shared red_accel S_WAIT/result path assumes 0. The caller does not need the value because it copies out[0..99] anyway.
**Suggested fix.** Set rd <- 0 per the ABI (or write pcpi_rd = 0), and remove 'rd = out[0]' from the pseudocode and the semantics.

## [MINOR] `fgemm10.s` — callability
**Finding.** Writing out[100..199] = 0 costs 100 extra bus beats on every call and buys nothing. It also forces the result into a scratch block and a separate copy-out, because a 200-word output cannot be aimed at C->elements (100 floats).
**Evidence.** 100 zero writes x 2 cycles = 200 cycles per call. A 200-word write into C->elements would overrun the struct, so the caller must copy 100 words back out, about 2,800 cycles at roughly 28 cycles per word. That copy-out is about 36% of the 7,740-cycle marshalling charge.
**Suggested fix.** If the ABI allows the output block to be shorter than the input block, declare out as 100 words and point rs2 straight at C->elements, which removes the copy-out. If both blocks must be `words` long, document the zero tail as required padding and keep the scratch copy.

## [MINOR] `fgemm10.s` — callability
**Finding.** rd receives out[0], which contradicts RED's fixed ABI ('rd <- 0') and gives the caller nothing it needs.
**Evidence.** The ABI states 'rd <- 0'. The semantics say 'rd receives out[0] (C[0][0]) with pcpi_wr=1', and the pseudocode ends with 'rd = out[0]'. The shipped red_accel template and any shared harness or Spike glue expect rd = 0.
**Suggested fix.** Set rd <- 0 (pcpi_rd = 0, pcpi_wr = 1), as the ABI requires.

## [MINOR] `fgemm10.s` — callability
**Finding.** The software cost per MAC is derived from host (x86) instruction counts, where a float multiply and add are single hardware instructions. On RV32IM without an FPU, they are __mulsf3 and __addsf3 soft-float calls.
**Evidence.** 'about 80 target cycles per MAC (9.4 host instructions per MAC x 8.5)'. Nine host instructions cannot cover two libgcc soft-float routines, which are tens of RV32 instructions each at 3 to 5 cycles per instruction on this core. The software baseline is therefore probably several times higher than 80 cycles per MAC, so the speedup is understated, not overstated. The rationale text is still wrong.
**Suggested fix.** Remove the 'soft-float call, about 80 target cycles' wording, or replace it with a measured RV32IM cycle count for one non-fused MAC through libgcc. If there is no measurement, state that 80 cycles is a conservative host-derived lower bound.

## [MINOR] `fgemm10.s` — benefit
**Finding.** The 200-word output block is half zero padding. It costs 100 useless write beats per call and stops the result from being written straight into C->elements, which forces a 100-word copy-back.
**Evidence.** out[100..199] = 0 is 100 beats = 200 cycles of pure waste per call. Because the block is 200 words, it would overrun C->elements[100] if aimed at C, so the caller must use a scratch buffer and copy 100 words back. Marshalling (7,740 cycles) is already about 78% of the accelerated per-call cost (7,740 out of 9,970 in the gate's own figure), so this is the dominant remaining term.
**Suggested fix.** Within the fixed ABI, document that the zero tail is required. Also declare the scratch output buffer and copy-back in marshal_words: the declared 300 x 10 = 3,000 is well below the 7,740 the gate measured. If the ABI ever allows output words != input words, write out[0..99] directly into C->elements and drop the copy-back.

## [MINOR] `fgemm10.s` — benefit+legality
**Finding.** The binary32 multiply-add datapath counts as 'large' by this core's own area threshold (it adds a second multiplier), and the spec does not quantify that area.
**Evidence.** The context defines large as '>= 32 words of state, or a second multiplier'. A 24x24 significand multiplier plus an align/normalize/round adder is a second multiplier array, on top of the measured +74% core area of the memory-operand coprocessor harness. The roughly 5 to 8x whole-application gain justifies it, but the spec says only that it is 'not a duplicate' of fast_mul.
**Suggested fix.** Add an area estimate to the description (multiplier array, adder/normalizer, and the 1-, 10- or 100-word operand buffer, whichever is chosen) so the area-versus-speedup trade can be judged.

## [MINOR] `fgemm10.s` — legality
**Finding.** Writing out[100..199] = 0 adds 100 useless stores, about 200 bus cycles per call, and forces the caller to allocate a 200-word output buffer when the useful result is 100 words.
**Evidence.** The semantics say: 'out[100] through out[199] are each written with 0x00000000'. The caller copies only out[0..99] into C->elements. The caller cannot pass C->elements (float[100]) directly as rs2 without overrunning the struct by 400 bytes.
**Suggested fix.** Keep it if the fixed ABI requires an output block exactly `words` long. Otherwise state the 200-word output buffer requirement as part of the caller contract in the fallback/use paragraph, so no caller passes C->elements directly.

## [MINOR] `fgemm10.s` — legality
**Finding.** rd is written with C[0][0], but RED's fixed operand ABI says rd <- 0, and the C model does not model rd at all.
**Evidence.** The pseudocode ends with 'rd = out[0]', and the semantics say 'rd receives out[0] (C[0][0]) with pcpi_wr=1'. The ABI says 'rd <- 0'. The c_model signature only produces out[].
**Suggested fix.** Set rd <- 0 to match the ABI, or document the deviation and have the C model and Spike model return rd so the two can be checked against each other.

## [MINOR] spec-wide — legality
**Finding.** The spec's top-level bookkeeping is inconsistent with the claim that it has been verified.
**Evidence.** 'encodings': [] is empty even though one encoding is defined. 'spike_model' is the empty string. passes_link1/passes_gate2 are false, and link1_vectors and gate2_vectors are 0.
**Suggested fix.** Fill in encodings with {custom-0 0x0B, funct3 000, funct7 0000000}, attach the Spike model, and record the link-1 and Gate-2 results.
