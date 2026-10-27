# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 0 blocking, 9 major, 15 minor

## [MAJOR] `xfop.hi` — implementability
**Finding.** The gate prices xfop.hi at 34 cycles (30 issue + 3 MAC), but the stated datapath needs about 3 x 16-20 = 48-60 cycles of arithmetic, so its latency is real and arithmetic-dominated.
**Evidence.** The spec describes one sequential multiply-add unit (12 radix-4 steps plus align/add/normalise/round, 16-20 cycles) that xfop.hi runs three times. The gate charges mac_ops x 1 cycle. PCPI issue for a register form is about 3 cycles (the calibration figure), not 30. Honest hardware latencies are about 21 for lo, about 57 for hi and about 4 for rd. That gives 250x21 + 167x57 + 70x4 ≈ 15.0k cycles per call, against the gate's 250x31 + 167x34 + 70x43 = 16.4k. The totals nearly cancel, so the 1.98x verdict is about right (about 2.0x honest) but for opposite reasons per instruction. Per k step the unit takes about 78 cycles for 4 MACs, against about 35 cycles of residual software (4 loads at 5 cycles plus pointer and loop ops), so the unit's arithmetic is what sets the cost. The design has no me
**Suggested fix.** State the real latencies in the spec (lo ~21, hi ~57, rd ~4) so downstream numbers are honest. Do not widen the datapath to three multipliers; that triples area for at most ~30 cycles per k step. A radix-8 multiplier step would cut about 4 cycles per pass, but only if the area allows.

## [MAJOR] spec-wide — implementability
**Finding.** The implied area is about 350-400 flops plus a 24-bit alignment shifter, a leading-zero normaliser and a 48-bit adder, which is 3-4x the 'small' bar (under 100 flops) and larger than the core's 160-flop divider.
**Evidence.** Architectural state alone is 6 x 32 = 192 flops. The sequential binary32 datapath adds roughly: two unpacked operands (~50), a 48-bit product/partial-product register, an addend/align register (~26), a result register (~32) and an FSM counter (~6). That is about 160-200 more flops. The core's own flops are roughly 1,800 (1,024 regfile + 128 counters + ~300 control + ~200 fast_mul + ~160 div), so this adds about 20% flops. With the shifter, normaliser and subnormal handling the area is probably +30-40%, against +74% for the shipped red_accel. The core's existing 33x33 fast_mul is internal and cannot be borrowed over PCPI, so a second multiplier datapath is built. The radix-4 sequential 24x24 avoids a second array, but it still duplicates the significand-multiply capability fast_mul already 
**Suggested fix.** Keep FLAT at 2 words and 2x2 blocking, since a larger block raises state and area for little gain. Share the 48-bit adder between the multiplier accumulate and the sum add. Use one normaliser pass for all three hi sums. Record the flop budget in the spec.

## [MAJOR] spec-wide — callability
**Finding.** The 1.98x prediction compares ~16.1k cycles of xfop instructions against the full ~59.5k cycles of replaced software, but the caller's remaining loop work is not in the 16.1k. That work is the operand loads, pointer arithmetic, loop branch, and the stores of drained results.
**Evidence.** The spec keeps 'the loads of A and B and the address arithmetic in software'. Per 2x2 k step that is 4 lw (~20 cycles), about 3 pointer adds (~9) and a counter add plus taken branch (~8), so ~37 cycles x 167 steps = ~6.2k. The 83 1x1 edge steps cost ~24 each, ~2k. About 70 drains with sw and addressing cost ~0.7k. The residual is roughly 9k, so the kernel is ~25k, not 16.1k. That gives ~2.4x on multiply and ~1.65x on the application if the gate does not already charge this. The 31-cycle and 34-cycle figures are issue-plus-MAC only.
**Suggested fix.** Have the gate (or the designer's own estimate) charge the residual loads, pointer arithmetic and loop control. Then reduce them: unroll k by 2 to halve the branch/counter cost, pre-scale the B row stride, and keep pointers in registers. If the verdict is still short, consider letting xfop.lo/hi take more of the per-step bookkeeping off the core, or a larger block where the padding cost is acceptable.

## [MAJOR] spec-wide — callability
**Finding.** The mechanical checks can see only the single-instruction rd output, so nearly all the architectural behaviour is unverified. That covers the accumulator updates, the LA/LB latch, the cross products LA*b1 and a1*LB, the xfop.rd select/clear logic, and sel 7. The spec itself has gate2_vectors 0, link1_vectors 0, tests [] and an empty spike_model.
**Evidence.** Gate 2 and link 1 run each instruction alone from reset. xfop.lo's rd is p = a0*b0 and xfop.hi's rd is p11 = a1*b1; neither reveals C00, C01, C10, C11, p01 or p10. xfop.rd alone from reset always returns 0. The spec says as much: 'Gate 2 cannot see' the interaction through the state.
**Suggested fix.** Add a sequence-level differential test, run on both the C models and the RTL. The worked 2x2x2 example, the special-value example, the unfused-rounding vectors, a drain-then-reuse case, and sel 4/5/6/7 should all be asserted on the drained C00..C11 values. Also add a matrix-level check against the soft-float ijk loop on the 10x10, 8x12, 5x20 and 9x9 shapes. Fill in spike_model with the state.

## [MAJOR] `xfop.lo, xfop.hi` — benefit
**Finding.** The performance gate prices each MAC at 1 cycle, but the spec's own datapath takes 16-20 cycles per multiply-add, so the 1.98x prediction is overstated.
**Evidence.** Spec: 'radix-4 sequential 24x24 multiplier... about 16-20 cycles per multiply-add' and 'Gate 3 prices a MAC at 1 cycle'. Gate prices: lo 31, hi 34. With ~18 cycles per MAC: lo ≈ 30+18 = 48 and hi ≈ 30+3x18 = 84. Per call that is 250x48 + 167x84 = 12.0k + 14.0k = 26k cycles. The gate counted 250x31 + 167x34 = 13.4k. xfop.hi alone is about 14k cycles per call. Hi remains faster than the ~238 software cycles for 3 MACs (about 2.8x), but the gate's 10.4x is not real. The spec's claim that 'the win still holds' is asserted without this arithmetic.
**Suggested fix.** Fix a target latency for the multiply-add, and charge it. Either raise mac_ops to the real cycle count (lo ≈ 18, hi ≈ 54), or add a parallel datapath. Re-run the gate with the honest latency and report that figure. If the 28-30 cycle issue overhead already overlaps the datapath, say so explicitly with the cycle trace.

## [MAJOR] spec-wide — benefit
**Finding.** The 16.1k-cycle estimate omits all the software the spec keeps (A/B loads, address arithmetic, loop control, C stores, clearMatrix), so the application bound is too high.
**Evidence.** Spec: 'The loop nest, the loads of A and B and the address arithmetic stay in software.' Per k-step of a 2x2 block: 4 loads x5 = 20, about 4 ALU ops x3 = 12 (A pointers, B stride add, k), and a taken branch ≈ 5, so about 37 cycles. 167 blocked steps x 37 ≈ 6.2k. 83 edge steps x ~24 ≈ 2.0k. 68 drain stores with address ≈ 0.55k. The 100-store clearMatrix is not replaced and costs about 1.5k. Residual ≈ 9-10k cycles per call. Against the credited 59.5k software, the gate's own instruction prices plus residual give 25.9k, or 2.3x on the kernel and about 1.63x on the application. With the honest latency above (≈28k for the three instructions plus ≈9.5k residual ≈ 37.7k) the kernel is 1.58x and the application is 1/(0.317+0.683/1.58) ≈ 1.33x. Even with zero residual the application is capped nea
**Suggested fix.** Report the application speedup including the residual loop and load cost. State plainly that the extension reaches about 1.3-1.6x, not 1.98x. To raise it, cut issue count per MAC, since 0.65 instructions x ~30 cycles ≈ 20 cycles of pure issue per MAC. For example, add a 4-row/4-col block with 8 operand values in 4 instructions for 16 MACs. This needs about 20 words of state and exceeds the ~100-flop 'small' budget, so weigh it against area.

## [MAJOR] `xfop.lo` — benefit
**Finding.** The odd-edge 1x1 use of xfop.lo is at or near break-even, and edge elements are a large share of the 5x5 and 9x9 shapes.
**Evidence.** Edge step: 2 loads (10) + ~15 address/loop + xfop.lo (31 by gate, ≈48 honest) ≈ 56-73 cycles per MAC. Software is 59.5k/749 ≈ 79 cycles per MAC. Ratio ≈ 1.1-1.4x. Edge MACs: 5x20x5 has 180 of 500 (36%); 9x9x9 has 153 of 729 (21%); 83 of 749 on average. These shapes are half the profile rotation. In the 1x1 path nothing is reused, so the 'one 2-source instruction = 1 MAC' penalty from round 1 returns.
**Suggested fix.** For an odd last row or column, use a 2x1 or 1x2 block: xfop.lo then xfop.hi with a zero second operand. That gives 2 MACs for 2 instructions but 3 loads instead of 4, and the kernel stays branch-simple. State the edge cost separately in the report. Do not count it at the 2x2 ratio.

## [MAJOR] `xfop.hi` — legality
**Finding.** The spec has `tests: []`, yet three of its four accumulator paths and the whole xfop.rd data path cannot be seen by any single-instruction Gate 2 vector, so a Gate 2 pass says almost nothing about them.
**Evidence.** Gate 2 runs each instruction alone from reset, so LA = LB = 0. Then p01 = LA*b1 and p10 = a1*LB are always ±0, and the sums stay +0. xfop.hi's only observable output is a1*b1, and xfop.rd alone only ever returns 0. A swapped cross term (LA*b1 vs a1*LB), a C01/C10 mix-up, wrong sel decoding, or a latch that is not written would all pass Gate 2. The spec itself says 'Gate 2 cannot see' this and relies on the worked sequences, but none of them is in `tests`.
**Suggested fix.** Put the worked sequences in `tests` as multi-instruction vectors: the 2x2 two-k-step sequence draining sel 0..3 with 0x40600000/0xC0400000/0x3FC00000/0x40A00000, the Inf/0 special-value sequence, a lo then rd sel 4/5 latch check, and a sel 7 followed by sel 0..5 returning 0. Add an asymmetric case such as A=[a0,a1] with a0≠a1 and B=[b0,b1] with b0≠b1, so that swapping LA and LB changes the result. The RTL testbench and any Spike model must run these in sequence, not per instruction.

## [MAJOR] spec-wide — legality
**Finding.** The spec-wide claim that results are 'bit-identical to the original loop' is only true for non-NaN data, because the NaN rule (always 0x7FC00000) is not what the soft-float code being replaced does.
**Evidence.** Description and xfop.hi semantics: 'the results are bit-identical to the original loop'. NaN rule: 'every NaN becomes 0x7FC00000, payload and sign not propagated'. On rv32 without F, the original `c + a*b` goes through libgcc __mulsf3/__addsf3, which in general propagates an operand's NaN payload or sign and does not canonicalise. The host-compiled profile build uses SSE, which also propagates payloads. The vector 0xFFC12345 * 1.0 -> 0x7FC00000 shows the difference.
**Suggested fix.** Narrow the claim to 'bit-identical for all non-NaN inputs and results; any NaN result is the canonical 0x7FC00000'. State that NaN payloads and signs are not reproduced. Check that the matrixmul test and the profile harness only use finite operands. If a NaN-sensitive consumer exists, say so in the spec.

## [MINOR] `xfop.rd` — implementability
**Finding.** The gate charges xfop.rd 43 cycles because its mask-loop c_model executes 91 instructions (priced as 13 sequential steps), while the hardware is a 6-way select plus a clear of about 3-4 cycles.
**Evidence.** The gate output says 'its C model executes 91 instructions ... charged as 13 sequential logic steps'. The spec's own timing note says 'a 6-way select and a clear'. The over-charge is about 70 x 13 = 910 cycles per call. Against a baseline of 59.5k/0.683 ≈ 87.1k cycles, that moves the predicted speedup from 1.98x to about 2.02x.
**Suggested fix.** Rewrite the c_model as a straight indexed read and clear (clamp sel, index a single 8-entry array of accumulators and latches) so the measured instruction count reflects the mux. Keep the branch-free behaviour.

## [MINOR] `xfop.lo` — implementability
**Finding.** Fixed latency is only an RTL promise, and a sequential datapath with a normalise or round step is where data-dependent timing creeps in.
**Evidence.** The spec says 'no early-out' for zero, Inf, NaN or subnormal, but the 16-20 cycle figure is non-normative and the mechanical timing checks cannot see RTL. The advisory in the gate output covers the same risk.
**Suggested fix.** Make it normative that the unit always runs the 12 multiply steps plus a fixed number of align and normalise cycles, with no early termination on zero, Inf, NaN or subnormal operands. Add this to the RTL test checklist.

## [MINOR] `xfop.hi` — implementability
**Finding.** The state-carrying lo/hi pair works only if the compiler keeps the two instructions in order and does not drop their discarded results.
**Evidence.** Both are issued with rd=x0, so the compiler sees no output. The correctness of xfop.hi depends on LA/LB written by the immediately preceding xfop.lo, and the xfop.rd drains depend on both having run.
**Suggested fix.** Document that the kernel must issue all three through volatile inline asm in source order, one k step per asm statement pair, with the entry and exit xfop.rd x0,x0,7 scrubs.

## [MINOR] `xfop.hi` — callability
**Finding.** The gate prices xfop.hi at 34 cycles, but the described datapath needs three back-to-back multiply-adds at 16-20 cycles each.
**Evidence.** 3 x 16-20 = 48-60 cycles plus ~3 for issue, so ~51-63 against the 34 charged. xfop.lo (31 charged vs ~19-23 real) and xfop.rd (43 charged vs a few cycles for a mux and clear) are overcharged. Per call the two errors roughly cancel (~16.4k charged vs ~15.6k honest), so the verdict does not flip. The datapath is non-normative, so nothing pins the latency.
**Suggested fix.** State a normative fixed latency for each instruction (for example lo = N cycles, hi = 3N, rd = 2) and price the gate from those numbers. If area allows, consider a second multiply-add lane so xfop.hi does not serialize three of them.

## [MINOR] `xfop.lo` — callability
**Finding.** The claim that results are 'bit-identical to the original loop' holds for non-NaN data only. The libgcc soft-float routines the application calls today propagate the operand NaN with its payload and sign, while xfop canonicalises every NaN to 0x7FC00000.
**Evidence.** The spec chooses RISC-V canonical NaN semantics, which is a different rule from libgcc's fp-bit/soft-fp behaviour. The two differ for any NaN operand with a non-canonical payload or sign. Whether libgcc's NaN output matches 0x7FC00000 for Inf*0 depends on the build.
**Suggested fix.** State in the spec that equivalence is guaranteed only for inputs with no NaN, or match the target libgcc behaviour. Confirm that the profile harness's matrices contain no NaN.

## [MINOR] `xfop.rd` — callability
**Finding.** The decoder ignores imm[11:3] and rs1, so 511 encodings alias each sel value. The spec also lists funct7 as '0000000' for an I-type instruction whose upper bits are not a funct7.
**Evidence.** 'imm[11:3] (bits 31:23) are ignored', and the decoder matches only opcode 0101011 with funct3 = 001. xfop.rd with imm=8 behaves as sel 0 and clears C00, so a stray encoding silently drains state.
**Suggested fix.** Decode imm[11:3] == 0 and treat the rest as illegal (CATCH_ILLINSN traps), or document the aliasing explicitly. Describe the encoding as I-type with no funct7.

## [MINOR] spec-wide — callability
**Finding.** The area cost is not quantified, and the state alone exceeds the 'small' budget.
**Evidence.** 'Small' is under ~100 flops. FACC and FLAT are 192 flops. The sequential binary32 multiply-add adds operand and exponent registers, a 48-bit product, an alignment/normalise shifter and a control FSM. A rough estimate is 350-450 flops in total, comparable to the fast_mul and divider combined. The spec's 'a fifth of the register file' covers only the 192 state bits.
**Suggested fix.** Give a flop and LUT estimate for the unit and compare it with the +74% red_accel baseline, so the 1.98x speedup can be weighed against its area.

## [MINOR] `xfop.hi` — callability
**Finding.** The lo-then-hi pairing contract is hidden state that the compiler cannot see.
**Evidence.** xfop.hi uses the LA/LB latched by the preceding xfop.lo. A C compiler, or a hand-written asm block without explicit ordering, could reorder, hoist or duplicate the two instructions. A stale latch would then give wrong results silently, because no register dependence ties them together.
**Suggested fix.** Specify the intended use as a single asm volatile block (or .insn macro) that issues lo then hi together, and say so in the spec. Optionally make the pair robust by having the unit invalidate the latch after each xfop.hi.

## [MINOR] `xfop.rd` — benefit
**Finding.** The 43-cycle price and the 19.93x ratio for xfop.rd are artefacts of the gate's model, not of the hardware.
**Evidence.** The gate charged 13 'sequential logic steps' because the C model runs 91 instructions. The spec says the hardware is a 6-way select and clear, so the true cost is ≈ 31 cycles. xfop.rd replaces no software at all (it is a data move that exists only because FACC is hidden state). Its 849-cycle 'software' credit is a pro-rata share of the kernel. Real cost: 70 x 31 ≈ 2.2k cycles per call, plus 68 stores. With 4 drains per 2x2 block, each paying a ~30-cycle issue, drain is about 15% of the accelerated cost.
**Suggested fix.** Present xfop.rd as overhead of the blocking scheme, not as a 20x instruction. Keep the price at about 31 cycles. Do not use its ratio when judging the extension.

## [MINOR] spec-wide — benefit
**Finding.** The software baseline is host Ir x 8.51, so the 59.5k figure is not a measured RV32 soft-float cost, and the speedup numbers rest on it.
**Evidence.** The hot-loop report was profiled with callgrind on the host. 6,987 Ir per call is ≈ 9.3 host instructions per MAC (about 80 cycles at 8.51 cycles per Ir). On a core with no F extension, fmul and fadd are libgcc calls, each tens of instructions. The 8.5 cycles per Ir factor is also a core-wide average, not a figure for this loop. The true software kernel is probably well above 59.5k, which would help the extension, but the stated 1.98x, 3.7x and my adjusted figures are all unverified against it.
**Suggested fix.** Time the unmodified multiply kernel on picorv32 with libgcc soft-float (rdcycle) and use that as the baseline in the ratios.

## [MINOR] spec-wide — benefit
**Finding.** The area cost is not quantified, and it is large relative to the gain.
**Evidence.** State is 192 flops (4x32 FACC + 2x32 FLAT). On top of that comes a binary32 multiply-add datapath with a 24x24 multiplier sequencer, alignment and normalisation shifters, and a rounder, plus the decode. That likely exceeds fast_mul's ~200 flops and approaches the divider's ~160, well beyond the 'small' budget of ~100 flops. The gain is ~1.3-1.6x.
**Suggested fix.** Add a flop and LUT estimate for the unit. Weigh it against the corrected 1.3-1.6x application speedup, and drop FLAT if the lo/hi pair can be restructured so state is not needed.

## [MINOR] `xfop.lo, xfop.hi` — benefit
**Finding.** The call contract depends on hidden state and strict adjacency, and nothing says how a C caller can emit it safely.
**Evidence.** xfop.hi reads LA/LB written by the preceding xfop.lo, and FACC is not visible to the compiler. If the two are emitted as separate non-volatile asm statements, or the compiler interleaves two k-steps, results are wrong. Gate 2 runs each instruction alone from reset and cannot see this.
**Suggested fix.** Specify that the kernel emits each lo/hi pair as one asm volatile block (or two volatile asms with a memory clobber), and add a sequence test (the worked 2x2 example) to the testbench.

## [MINOR] `xfop.rd` — legality
**Finding.** Selectors 4, 5 and 6 serve no kernel use and make the C model about 91 instructions, which the performance gate prices at 43 cycles (13 sequential steps) instead of roughly the 31-cycle floor.
**Evidence.** The kernel schedule only issues sel 0..3 and sel 7. The LA/LB readout with clear and the reserved sel 6 appear only in tests. The branch-free model visits all 6 elements for every sel. At x70 per call, 43 vs 31 cycles is about 840 of the roughly 16.4k predicted kernel cycles, around 5%.
**Suggested fix.** Drop sel 4/5 readout and sel 6. Keep sel 0..3 as a direct bounded index (`xf_acc[sel & 3]`) and keep sel 7 as clear-all. Make every other sel value return 0 with no state change, so the instruction stays total. The model then executes about 10-15 instructions, and the hardware mux shrinks.

## [MINOR] `xfop.rd` — legality
**Finding.** The flat `funct7: '0000000'` misdescribes the real decode: the decoder matches opcode and funct3=001 only, so bits 31:25 are don't-care and sel values above 7 alias.
**Evidence.** Semantics: 'imm[11:3] (bits 31:23) are ignored... the decoder matches opcode 0101011 with funct3 = 001 only.' The (opcode, funct3, funct7) triple used by Gate 2's collision check records (custom-1, 001, 0000000), so it does not show that the instruction consumes the whole funct3=001 slot of custom-1, 128 funct7 values. Any later instruction at funct3=001 would silently collide in the hardware while passing the check. imm[4:3] in instruction bits 24:23 are also ignored, so `xfop.rd rd, x0, 8` executes as sel 0.
**Suggested fix.** Either make the decoder check bits 31:23 == 0 and trap otherwise, which leaves the rest of funct3=001 free, or record in the spec that funct3=001 is reserved in full for xfop.rd. The first is cheaper and safer, and the encoding string should then show imm[11:3]=000000000.

## [MINOR] `xfop.hi` — legality
**Finding.** The spec gives no normative latency or area for the FP datapath, and its own caveat says Gate 3's pricing of xfop.hi (34 cycles) is well below what the stated hardware would take.
**Evidence.** The non-normative datapath is a sequential multiply-add at about 16-20 cycles. xfop.hi does 3 of them, so about 48-60 cycles plus issue, against 34 priced. xfop.lo is priced at 31 against about 16-20 plus issue. These errors partly cancel, because the issue cost for a register-form PCPI is 3 cycles per the core table but the gate charges about 30. The net is within a few percent of the 16.4k estimate, but only if the issue overhead is as the gate assumes. State is 192 flops, plus a 24x24 radix-4 multiplier, a 48-bit product/aligner register and a normaliser, probably 300+ flops in total. That is over the 'small' bound of 100 flops, and the spec gives no figure for it.
**Suggested fix.** State per-instruction latency as a normative fixed number (for example lo = 18, hi = 54 cycles after pcpi_valid, with pcpi_wait raised on the first cycle), and give an estimated flop count for the unit. Alternatively, share one multiply-add across lo and hi so that the latency is the same for both. Re-run the Gate 3 comparison with those figures so the 1.98x is not resting on the MAC=1-cycle assumption.
