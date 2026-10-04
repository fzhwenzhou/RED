# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 0 blocking, 14 major, 11 minor

## [MAJOR] `secp256r1.mmod` — implementability
**Finding.** The instruction always writes 16 output words, but micro-ecc's mmod_fast result buffer holds only 8 words, so the caller cannot pass its own `result` pointer in rs2.
**Evidence.** The semantics say "out[0..7] = X mod p ... out[8..15] = 0", and the ABI sets `words`=16. In uECC_vli_modMult_fast, `result` is a uECC_word_t[8] (num_words_secp256r1 = 8 on RV32), so out[8..15] would write 32 bytes past it on the stack. Gate 4 cannot see this because it runs the model on a 16-word buffer. The rule "in and out must not overlap" also stops the caller from reusing the 16-word `product` buffer as the output. Every call therefore needs a 16-word scratch block and an 8-word copy back. That copy is what marshal_words=8 is paying for (206 cycles of marshalling), and the 8 zero stores add 16 bus cycles to every call.
**Suggested fix.** Say plainly in the semantics that rs2 must point to a 16-word buffer and that a uECC 8-word `result` must never be passed directly. Also allow in-place use (rs2 == rs1), which costs nothing if the unit reads all 16 input words before writing any output. With that, the caller can reduce `product` in place and copy out[0..7] once; the scratch block disappears and the marshalling falls to the 8-word copy-out alone.

## [MAJOR] `secp256r1.mmod` — implementability
**Finding.** The hardware note leaves room for a 16-word input buffer plus an 8-word R register file. That is 768 flops, about 78% of the 992-bit register file, and it buys almost nothing given the latency headroom.
**Evidence.** The note says the passes "stream the fixed source words from the input block (bus master) or a 16-word buffer, R lives in out[] or 8 registers". Holding everything costs 512 + 256 flops. Streaming instead costs about 70 re-reads of the nonzero term words, about 140 bus cycles per call (at about 2 cycles each). With no buffer, the instruction takes roughly 28 + 64 + 140 + 80 (ten passes × 8 limbs) + 128 (eight corrections, each a borrow pass then an apply pass) ≈ 440 cycles. The gate charged 641 per invocation against 34,754 cycles of software, so the faster buffered option is not needed.
**Suggested fix.** Specify the area-minimal datapath: R in 8 registers (256 flops), terms re-read from in[] over the bus for each pass, one 33-bit adder and a 4-bit t. Each correction step is a borrow-only pass followed by an apply pass, so A and D are never both stored. That avoids another 512 flops of A/D buffering, which the current wording ("computes both A and D and selects with masks") implies when taken literally in hardware.

## [MAJOR] `secp256r1.mult` — implementability
**Finding.** mac_ops=64 means 64 single-cycle 32×32 multiply-accumulates, which needs a second full 32×32 multiplier on a core that already has one. The cheaper multiplier the spec itself allows would take about 4× as many arithmetic cycles.
**Evidence.** The cost model charges 1 cycle per mac_op. Doing 64 MACs in 64 cycles needs a 32×32→64 array (one DSP, or about 1k LUTs without one), which duplicates picorv32_pcpi_fast_mul's 33×33. The duplication is forced, because the accelerator cannot issue to the internal PCPI mux. The semantics allow "a 32x16 or 32x8 multi-cycle multiplier"; with 32×8, the arithmetic takes 4 × 64 = 256 cycles. The latency then becomes about 28 + 64 (32 beats) + 128 (re-fetching A[i] for each partial product while B is held) + 256 ≈ 476 cycles. That is still about 18× better than the 8,748-cycle software, so the verdict does not change. But the declared figure only holds for the configuration with the most area.
**Suggested fix.** Pick the multiplier width in the spec. Recommended: 32×8 (or 32×16), with mac_ops declared as 256 (or 128) for honest latency. Otherwise keep 64 and state explicitly that the design spends a second DSP or 32×32 array to get it.

## [MAJOR] `secp256r1.mult` — implementability
**Finding.** The operand state is left open, and holding both A and B costs 512 flops (about half the register file) plus a 96-bit accumulator.
**Evidence.** Comba scheduling reads A[i] and B[k-i] in a pattern that is not unit-stride across columns. Holding both operands costs 512 + 96 flops with 32 bus beats (64 cycles). Holding only B costs 256 + 96 flops and adds 64 re-reads of A[i] (+128 cycles). Holding neither costs 96 flops and adds 128 re-reads (+256 cycles). The outputs can be written one column at a time, so no output buffer is needed in any of these cases.
**Suggested fix.** Make the B-only buffer normative: 256 + 96 + 32 (current A[i]) ≈ 384 flops at about 350–480 cycles, depending on multiplier width. State the flop count in the hardware note so the area figure can be checked.

## [MAJOR] `secp256r1.modsub` — implementability
**Finding.** Moving operands dominates this instruction's cost, and on its own it earns little: about 2% of application cycles. It is only worth building if it adds no dedicated state.
**Evidence.** The gate's figures are 158 cycles for the instruction plus 619 for marshalling, against 1,204 cycles of software (1.55×). The fixed overhead and traffic (28 + 64 = 92 cycles) outweigh the arithmetic (33 steps), and the marshalling (24 words: L and R copied into a contiguous block, then 8 words copied back) is 4× the instruction itself. The saving is about 427 cycles per call on a 6.2% hotspot, roughly 2.2% of total cycles. The streaming form described still needs either the 8 D limbs (256 flops) or a re-read and rewrite of out[0..7] (+32 cycles). Calls such as uECC_vli_modSub(t5, t5, X1) operate in place, which the no-overlap rule also forces through scratch.
**Suggested fix.** Require modsub to share mmod's 33-bit adder, the P256 constant and the R/limb registers, so its marginal area is just decode plus a borrow bit. Store D into out[0..7] and do the conditional add of p as a second read-modify-write pass, rather than adding a 256-bit D buffer. If it cannot share that datapath, drop it.

## [MAJOR] `secp256r1.mult` — callability
**Finding.** The spec does not fix the multiplier's width, so it can be built as a full 32x32 single-cycle multiplier, which duplicates picorv32_pcpi_fast_mul's DSP on a core whose constraints forbid exactly that.
**Evidence.** The semantics say 'the unit needs its own multiplier ... a 32x16 or 32x8 multi-cycle multiplier is acceptable (latency only)'. That is a permission, not a requirement. The context says 'A 32x32 or wider array multiplier duplicates the existing fast_mul and pays twice.' The latency budget allows a narrower multiplier: the gate charges 64 MAC cycles, a 32x16 multiplier adds about 64 cycles and a 32x8 about 192. Even at 32x8 the instruction costs about 490 + 413 cycles against 8,748 in software, still above 9x.
**Suggested fix.** Require the multiplier to be a 32x16 (or narrower) iterative multiplier with a 96-bit accumulator. Alternatively, mux red_core's PCPI operands into the existing picorv32_pcpi_fast_mul while the unit is busy, since the CPU is stalled then anyway. State that A[i] and B[k-i] are re-fetched per partial product, about 128 reads or 256 cycles, rather than buffering B in 256 flops. Then update the latency claim to match.

## [MAJOR] `secp256r1.mmod` — callability
**Finding.** The unit always writes 16 output words, but the caller's destination `result` holds only 8. If a caller passes `result` as rs2, the 8 words after it on the stack are overwritten with zeros. The spec never says the output buffer must be a 16-word scratch.
**Evidence.** In uECC_vli_modMult_fast the caller is `uECC_word_t product[2*num_words]; uECC_vli_mult(product,...); curve->mmod_fast(result, product);`, and `result` is num_words = 8 words. The spec says the unit 'writes all 16 words of out[]', with out[8..15] = 0. Overlap with the input is forbidden, so rs2 = product is not allowed either. marshal_words=8 already prices a scratch buffer plus an 8-word copy (about 206 cycles), but the semantics never state that requirement. A caller who skips the scratch gets silent stack corruption, which the C-model sanitizers cannot see because the model takes uint32_t out[16].
**Suggested fix.** Add to the semantics: 'rs2 must address a 16-word buffer; out[8..15] are clobbered. Callers with an 8-word destination must use a scratch block and copy out[0..7].' Show the call sequence: product -> scratch16 -> copy 8 words to result. If the fixed ABI allows a per-instruction output width, set the output to 8 words instead, which also saves 8 bus writes (16 cycles).

## [MAJOR] `secp256r1.modsub` — callability
**Finding.** modsub has the same 16-word-output trap as mmod, and it is worse here because micro-ecc almost always calls modSub in place, so even the scratch copy needs care.
**Evidence.** XYcZ_add/XYcZ_addC/double_jacobian call `uECC_vli_modSub(X2, X2, X1, p, n)` and similar, where result aliases left or right and every buffer is 8 words. To use the instruction, the caller copies L and R into a 16-word in-block (16 words) and points rs2 at a separate 16-word scratch. It cannot point rs2 at X2, which is 8 words, and out may not overlap in. It then copies 8 words back. That is the declared marshal_words=24 and the gate's 619 marshalling cycles against 158 cycles for the instruction itself. The routine works and pays (1.55x), but none of this is stated in the semantics.
**Suggested fix.** Document the required caller sequence in the semantics: a 16-word scratch for in, a 16-word scratch for out, then copy out[0..7] to result. That sequence works even when result aliases left or right. State explicitly that rs2 must never be the caller's 8-word result buffer.

## [MAJOR] `secp256r1.mmod` — callability
**Finding.** The hardware note leaves the storage plan open. The obvious build (a 16-word input buffer plus 8-word R registers plus A/D temporaries for the correction steps) adds about 768 to 1,024 flops, which is more than the whole register file on this core.
**Evidence.** The note says the passes 'stream the fixed source words from the input block (bus master) or a 16-word buffer, R lives in out[] or 8 registers'. Each correction step picks A or D using b, the final borrow of the whole 8-word subtraction, so a single-adder streaming design must either keep D (256 flops) or run two passes. 16 words (512) + R (256) + D (256) = 1,024 flops, against a 992-bit register file. The context calls 512 flops 'HUGE'. A minimal plan fits easily in the 34,754-cycle software budget: R in 8 registers (256 flops), c[8..15] re-fetched over the bus each pass (≤80 reads ≈ 160 cycles), and each correction step done as one compare/borrow pass followed by one masked add-or-subtract pass. Total is about 300 to 400 cycles.
**Suggested fix.** Specify that minimal-area plan in the semantics: R held in 8 registers, source words re-read from in[] over the bus each pass (no input buffer), and two passes per correction step instead of storing both A and D. Optionally replace the 8 correction steps with an add of t·p from a small table, since t is in [-4, 6], followed by ≤2 constant-time corrections. That cuts the correction work from 8 steps to about 3.

## [MAJOR] `secp256r1.modsub` — benefit
**Finding.** Marshalling takes up most of the cost. modsub pays for itself only 1.55x, and its 6.2% share becomes the largest covered term left in the Amdahl sum.
**Evidence.** Instruction cost = 28 + 2x(2x16) + 0 = 92 cycles (the gate charged 158 including 33 logic steps). marshal_words=24 adds 240 by formula, and the gate measured 619. That gives 777 cycles against 1,204 in software, so 1.55x, and about 80% of the cost is copying L and R into a contiguous block and copying 8 words back. Amdahl: the residual is 0.028 uncovered + 0.425/12.32 + 0.485/41.03 + 0.062/1.55 = 0.028 + 0.0345 + 0.0118 + 0.0400 = 0.114, which gives about 8.75x and matches the gate's 8.72x. modsub's 0.040 is larger than mult's 0.0345 even though it covers a seventh of the cycles. If modsub cost no marshalling it would run at about 1,204/250 = 4.8x, and the whole application would reach about 1/(0.028+0.0345+0.0118+0.0129) = 11.4x.
**Suggested fix.** Cut the marshalling on the caller side. In XYcZ_add, XYcZ_addC and double_jacobian, allocate the coordinate temporaries that modSub pairs as adjacent 8-word halves of 16-word blocks, so in[0..7]=L and in[8..15]=R already sit in place. Point rs2 at the destination, which can be the next block's half if its upper 8 words are scratch. That brings marshal_words close to 0-8. If the call sites can't be rearranged, record that the 1.55x is mostly copy overhead and leave the instruction in, because it shares mmod's 33-bit adder and P constant and so adds almost no area.

## [MAJOR] `secp256r1.mult` — benefit
**Finding.** The latency model charges 1 cycle per MAC, which assumes a full single-cycle 32x32 multiplier inside the accelerator. The semantics, though, invite a 32x16 or 32x8 multi-cycle multiplier. The latency and the area cannot both be what the spec suggests.
**Evidence.** mac_ops=64 at 1 cycle each gives 92+64 = 156 cycles of datapath (the gate charged 297). PCPI cannot drive picorv32_pcpi_fast_mul, so the unit needs its own multiplier. The core profile says a duplicate 32x32 'pays twice' (a second DSP or array). With the suggested 32x8 multiplier, each MAC costs 4 cycles, so 256 MAC cycles, about 297-64+256 = 489 + 413 marshalling = 902 against 8,748, which is 9.7x. With 32x16 it is about 233 + 128 + 413 = 774, so 11.3x. Either way it still pays a lot, but the declared figure is the full-multiplier figure.
**Suggested fix.** Choose a multiplier width and state it. 32x16 (2 cycles per MAC) is the sensible point: it is about a quarter of a 32x32 array and costs under 10% of the gate-credited speedup. Then make the semantics' hardware note and the cost model agree, either cycles per MAC = 2 or mac_ops counted as 128 half-MACs.

## [MAJOR] `secp256r1.mmod` — benefit
**Finding.** The 41x per-call ratio rests on a call count that cannot be right for this kernel. That inflates the per-call software cost the instruction is measured against, even though the cycle share used in the Amdahl sum is sound.
**Evidence.** vli_mmod_fast_secp256r1 runs once after every uECC_vli_mult in uECC_vli_modMult_fast and modSquare_fast, which is 3,694,592 mult calls. The report lists only 530,967 mmod calls, most likely because inlining hides the rest. That implies about 34,754 to 69,000 cycles per reduction. The code actually does about 10 eight-word add/sub passes at roughly 770 cycles each, plus the shift/OR term construction and 0-2 corrections, so about 8,000 to 10,000 cycles on this core. Using about 9,000 cycles and 2x(92+516-ish logic)+206 ≈ 1,480, the realistic per-call ratio is about 6x, not 41x. The whole application then drops to about 1/(0.028+0.0345+0.081+0.040) = 5.4x. That is still a useful speedup, but it is well short of 8.72x.
**Suggested fix.** Count calls to vli_mmod_fast_secp256r1 with inlining disabled (-fno-inline on curve-specific.inc), or take them as equal to uECC_vli_mult's call count, and re-price the instruction against the per-call cost that results. Nothing in the instruction itself needs to change.

## [MAJOR] spec-wide — legality
**Finding.** The spec says in[] and out[] 'must not overlap' but never says what happens when they do, so all three instructions are undefined on part of their input domain (rs1, rs2).
**Evidence.** The description says 'in and out must not overlap; the instruction never writes the input block'. secp256r1.mult's hardware note also lets an implementation 'hold B (256 flops) and re-fetch A[i] per partial product, or re-fetch both'. With rs2 = rs1 (or rs2 = rs1+4 and so on), an implementation that re-fetches A after writing out[0] reads a word it has already overwritten. An implementation that buffers A does not. Two conforming units therefore give different results for the same (rs1, rs2). The same applies to mmod ('R lives in out[] ... or 8 registers') and to modsub. micro-ecc calls these routines in place all the time (uECC_vli_modSub(t5, t5, X1); mmod_fast(result, product) with result inside the caller's frame). So a caller that skips the scratch block is exactly the case that hits t
**Suggested fix.** Make the instruction total over rs1 and rs2. Either require every implementation to read all 16 input words before writing any output word (the result is then the function of the pre-instruction in[] for any overlap), or state that overlap gives an UNSPECIFIED but memory-safe out[] and that the unit still writes exactly out[0..15] and nothing else. Delete the 're-fetch A/both' latitude, or tie it to the non-overlap precondition explicitly.

## [MAJOR] `secp256r1.mmod` — legality
**Finding.** `invocations` = 2 is factually false. Per the semantics, one call of vli_mmod_fast_secp256r1 is exactly one instruction, so the declared field contradicts the spec's own prose and the gate charges the instruction's cost twice.
**Evidence.** The semantics say 'one call ... consumes exactly one 16-word product and produces one 8-word residue, so the data-derived count is 1; `invocations` is declared 2 as a disclosed conservative over-charge'. The perf gate prices it as '641 cyc ... x2 per call'. The field no longer describes the instruction; it describes a workaround for a write_spec heuristic.
**Suggested fix.** Declare invocations = 1. Fix the write_spec per-word bound so it accepts a compute-dense kernel whose C model runs 3,720 instructions on 16 words, rather than leaving a wrong value in the spec. If the tool cannot be changed this round, keep the disclosure but move it to a top-level note so that `invocations` stays a correct field across specs.

## [MINOR] `secp256r1.mmod` — implementability
**Finding.** `invocations`=2 is a declared over-charge of a single-call kernel, so the gate's 41× credit and its cycle accounting describe a call that doesn't exist.
**Evidence.** The semantics admit the data-derived count is 1 and that 2 is declared only to get past write_spec's per-word heuristic. The gate reports "641 cyc ... x2 per call".
**Suggested fix.** Correct the write_spec heuristic so it accepts invocations=1 for a compute-dense kernel, then declare 1. Until then, keep the disclosure and note that the true per-call cost is about 641 + 206 cycles.

## [MINOR] `secp256r1.modsub` — callability
**Finding.** The instruction hard-wires p256, but the routine it replaces takes `mod` as a parameter. A caller that passes any other modulus (curve->n, or another curve through the uECC_ENABLE_VLI_API export) gets wrong results with no error.
**Evidence.** The replaced routine is `uECC_vli_modSub(result, left, right, mod, num_words)`. The semantics say 'modulus hard-wired to p'. The profiled call sites all pass curve->p, but uECC_vli_modSub is a public symbol when the VLI API is enabled.
**Suggested fix.** Use the instruction only through a secp256r1-specific wrapper, for example in uECC_vli_modSub_fast / XYcZ paths that pass curve->p. Keep the generic uECC_vli_modSub in software, or guard it with `mod == curve_secp256r1.p`. State this in the semantics.

## [MINOR] `secp256r1.mmod` — callability
**Finding.** `invocations` is declared as 2, but the spec's own analysis says the true count is 1, so the gate charges this instruction twice its actual cost.
**Evidence.** The semantics say 'the data-derived count is 1; invocations is declared 2 as a disclosed conservative over-charge because write_spec's static per-word bound rejects 1'. The gate prices it 'x2 per call'. The verdict is still a 41x speedup, so this does not change the outcome. It does mean the reported 8.72x understates the real speedup.
**Suggested fix.** No change to the spec is needed. Fix the write_spec per-word heuristic so it accepts invocations=1 for compute-dense kernels, then re-declare 1 so the gate's figure is accurate.

## [MINOR] `secp256r1.mmod` — benefit
**Finding.** invocations=2 is a disclosed over-charge. The data-derived value is 1, so the gate double-counts 92 cycles of issue and bus traffic plus about 516 logic steps per call.
**Evidence.** The semantics say 'one call ... consumes exactly one 16-word product and produces one 8-word residue, so the data-derived count is 1; declared 2 ... because write_spec's static per-word bound rejects 1'. This is a known false positive in the tool's heuristic, not a property of the instruction. It makes the estimate pessimistic, not wrong.
**Suggested fix.** Keep it for now. Fix the per-word bound in write_spec so compute-dense kernels like this can declare invocations=1.

## [MINOR] `secp256r1.mult` — benefit
**Finding.** mult and mmod always run back to back in uECC_vli_modMult_fast and modSquare_fast. Running them as two instructions pays a second 28-cycle issue and moves 32 extra words over the bus on every field multiply.
**Evidence.** mult writes its 16-word product to out[] and mmod immediately reads the same 16 words back, which costs 2x16x2 = 64 bus cycles plus 28 issue cycles, about 92 cycles per field multiply across about 3.7M calls. A fused 'mulmod' would read 16 words, keep the 512-bit product internally (16 words, about 512 flops, which the profile calls HUGE for this core), and write 8.
**Suggested fix.** Leave the two separate. The 512-flop product buffer a fused version needs is out of proportion to about 92 cycles saved on a roughly 1,700-cycle pair. Do make sure the caller chains them with no copy in between (mult's rs2 equals mmod's rs1), so mmod's marshal_words stays at 8.

## [MINOR] `secp256r1.modsub` — benefit
**Finding.** The modulus is hard-wired to p256, but uECC_vli_modSub takes the modulus as an argument. The call-site replacement is only correct where mod == curve->p.
**Evidence.** The semantics say 'Exactly uECC_vli_modSub(result, left, right, p256) with the modulus hard-wired'. In micro-ecc the hot callers pass curve->p (the point formulas and verify), but the function is public and generic.
**Suggested fix.** Dispatch to the instruction in uECC_vli_modSub only when mod == curve_secp256r1.p, and fall back to software otherwise. Say so in the semantics.

## [MINOR] spec-wide — benefit
**Finding.** mmod and modsub produce 8 meaningful words but write all 16, so they spend 8 bus beats on zeros on every call.
**Evidence.** out[8..15] = 0 costs 8 words x 2 cycles = 16 cycles per invocation. For modsub that is about 10% of its 158-cycle instruction cost, and for mmod it is charged twice because invocations=2.
**Suggested fix.** If the fixed ABI allows words to differ for input and output, declare 16 in and 8 out for mmod and modsub. Otherwise accept the 16 cycles and note them.

## [MINOR] `secp256r1.mult` — legality
**Finding.** The hardware note lets the multiplier be 32x16 or 32x8 multi-cycle, but mac_ops = 64 and the gate's cost of 1 cycle per MAC assume a single-cycle 32x32 multiply. The specified latency and the permitted hardware disagree by up to 4x on the MAC term.
**Evidence.** The gate prices 297 cyc with 64 MAC at 1 cycle each. A 32x8 unit needs 4 cycles per partial product, so 64x4 = 256 cycles instead of 64, about +192 cycles, or roughly 490 cycles instead of 297. The 12x verdict against 8,748 cycles of software survives this, so it is not blocking. It does mean that the area budget (a second 32x32 multiplier duplicating fast_mul's DSP) and the timing claim are paired inconsistently.
**Suggested fix.** Pick one. Either commit to a 32x32 single-cycle multiplier and state its area as a second DSP, or commit to 32x16/32x8 and say the MAC term is 2x/4x 64 cycles in the semantics.

## [MINOR] spec-wide — legality
**Finding.** Address wrap-around is unspecified: nothing says what the unit does when rs1+60 or rs2+60 crosses 2^32.
**Evidence.** The ABI says only 'the low two address bits are ignored (treated as 0)'. The address arithmetic for in[15] and out[15] is not given modulo 2^32. A total spec should say whether the bus address wraps.
**Suggested fix.** State that word i is accessed at ((rsN & ~3) + 4*i) mod 2^32.

## [MINOR] `secp256r1.mmod` — legality
**Finding.** mmod and modsub always write 8 zero words to out[8..15]. This costs about 16 bus cycles per invocation and forces callers to use a 16-word scratch output even though the useful result is 8 words.
**Evidence.** The semantics say 'out[8..15] = 0' for both. micro-ecc's `result` for mmod_fast and modSub is an 8-word field element (often a point coordinate), so the instruction cannot write straight into it without overrunning 8 words. That is why marshal_words includes a copy-back. The zeros are well-defined, so this is a cost, not a defect.
**Suggested fix.** If the fixed ABI allows a per-instruction output word count, declare an 8-word output for mmod and modsub. Otherwise keep the zeros, and document that rs2 must name a 16-word buffer distinct from the caller's 8-word result.

## [MINOR] spec-wide — legality
**Finding.** The top-level fields are inconsistent with the instructions: `encodings` is [], every `spike_model` is "", and `tests` is [], while each instruction carries its own encoding and the review context claims an independent Spike model agrees.
**Evidence.** "encodings": [], "spike_model": "" on all three instructions, "passes_gate2": false, "gate2_vectors": 0.
**Suggested fix.** Fill `encodings` with the three custom-1 slots (0101011 with funct3 000/001/010 and funct7 0000000), attach the Gate 2 Spike models, and record the vector counts so the artifact matches what was verified.
