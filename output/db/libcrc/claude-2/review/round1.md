# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 0 blocking, 4 major, 12 minor

## [MAJOR] `crc64.f8h / crc64.f8l` — implementability
**Finding.** The pair is credited against the same crc_64_ecma software cost twice, so the loop's speedup is overstated and the prediction leans on it.
**Evidence.** Both instructions list replaces=crc_64_ecma::byte_loop, and Gate 3 prices each against 409 cyc of software per 8-byte chunk (7.77x and 7.79x). The 8-byte chunk is only done once in software, and the pair is needed to do it in hardware. Honest figures per chunk are 53+52 = about 105 cycles for the pair, plus about 24 cycles of lw/lw/xor/xor/addi/bne glue, against 409. That is about 3.2x for the loop, not 7.8x. If Gate 3 sums savings per instruction, it books about 2x2048x356 = 1.46M cycles saved against about 0.84M cycles of software (2048x409). That is more than the whole loop costs. The gate also charges 24 and 23 'sequential logic steps' rather than the engine's 8 cycles, which is inconsistent with the rationale's own datapath.
**Suggested fix.** Credit the pair once, as 105 cycles per 409-cycle chunk (or give crc64.f8l a different replaces, or none). Re-run Gate 3 and check the 2.19x still holds. The invocation count itself is right: 98,313 host instr / 6 per byte = 16,385 bytes, which is 2048 chunks, and the ~578 implied figure only reflects the gate's step-count heuristic.

## [MAJOR] spec-wide — implementability
**Finding.** The spec never states the area of the shared engine, and the 64-bit width needed by CRC-64 sets it for all eight instructions.
**Evidence.** The rationale describes a 64-bit CRC register, a 64-bit message shifter and 8 unrolled steps, each a 64-bit AND-XOR with a selectable polynomial. That is about 140 flops, which is 14% of the 1024-bit regfile and roughly a quarter of the CPU's 400-600 other flops. The logic is 8x64 AND/XOR, plus 64-bit input and output reflect/byte-swap/width muxes across 3 widths and 2 directions. I estimate roughly 700-1000 LUTs, about half the 1-2k-LUT CPU, with no multiplier. Six of the eight instructions need only a 32-bit state. The 64-bit half serves CRC-64, 6.75% of cycles. At the corrected ~3.2x, that saves about 4.6% of the application's cycles.
**Suggested fix.** State the area estimate in the spec. Consider a 32-bit engine for the six 16/32-bit instructions, with CRC-64 as a separate narrow-tap or two-pass datapath. Alternatively drop crc64.f8h/l if the 64-bit width alone pushes the engine toward red_accel's +74%. Polynomials are constants, so for the 16-bit CRCs only the 16 low taps need XOR gates per step.

## [MAJOR] `crc64.f8h` — benefit
**Finding.** The CRC-64 pair is worth about 3x on a 6.8% loop, not the 7.8x the gate prints, and it is the only reason the shared engine has to be 64 bits wide.
**Evidence.** The gate prices f8h at 53 cycles and f8l at 52, each against the full 409-cycle software chunk. Both run on every chunk, so the real cost is 105 cycles plus about 24 of caller overhead (lw, lw, 2 xor, addi, bne at 5+5+3+3+3+5). That is about 129 cycles against 409, so roughly 3.2x. Savings are 6.8% x (1 - 1/3.2), about 4.7% of cycles, or about 1.05x overall. The 64-bit register, 64-bit message shifter and 8x64 AND/XOR unroll exist only for this pair. The six other instructions need a 32-bit register.
**Suggested fix.** Either drop crc64.f8h/f8l and leave CRC-64 on the table loop, which shrinks the engine to 32 bits, or keep the pair and justify its area against about 1.05x. Do not rely on the 7.8x figure.

## [MAJOR] spec-wide — benefit
**Finding.** The shared engine's area is never quantified, and the 8-steps-per-cycle unroll is a large cost for a cycle saving the pricing barely sees.
**Evidence.** The rationale states about 140 flops plus 8x64 AND/XOR, which is roughly 500+ LUT3s, and a 7-polynomial mux, but gives no total. PicoRV32 without M is roughly 1-2k LUTs, and the prior red_accel cost +74%. The gate prices each f8 at 29 cycles against 477-545 of software, and the engine itself needs only 8 cycles. Going to 4 steps per cycle makes the engine 16 cycles, so about 37 cycles per f8 and still about 13x on the gate's basis (about 8x with caller overhead), while halving the AND/XOR array.
**Suggested fix.** Give a LUT/flop estimate for the engine. Unless the area budget is clearly fine, use a 4-step (or 2-step) unrolled datapath and re-run the performance gate with the longer latency.

## [MINOR] `crcsick.w` — implementability
**Finding.** The claim that all eight instructions share one shift-register engine does not hold for crcsick.w.
**Evidence.** Each of its steps does shift, conditional 0x8005 XOR, then XOR with (byte | prev_byte<<8), and carries a 24-bit packed state (crc + p). That is a different datapath from the plain shift-XOR CRC engine. It also has 4 sequential steps in 1 cycle, while the engine is described as 8 steps per cycle.
**Suggested fix.** Budget crcsick.w as its own small datapath: a 16-bit crc plus an 8-bit p, 4 stages, about 40 LUTs and 24 flops at most. Do not count it as free reuse.

## [MINOR] `crc32.f8 / crc16.f8 / crcdnp.f8 / crckermit.f8 / crcccitt.f8` — implementability
**Finding.** The gate's per-instruction cost (29 cyc, with a 28-cycle issue overhead) does not match what a register-form PCPI op on this core actually costs, and the caller glue is not shown.
**Evidence.** The engine runs 8 cycles, plus about 2 cycles of PCPI issue, so about 10-12 cycles for the instruction. Around it sit two lw at 5 cycles each, an xor at 3, an addi at 3, and a taken bne at 5, about 21 cycles of glue per chunk. Together that is about 31-33 cycles, so the 29 cyc figure is roughly right in total but for the wrong reason. The 477 cyc software baseline (7 instr/byte x 8 x 8.5) is consistent.
**Suggested fix.** Annotate that the 28-cycle overhead stands in for the lw/xor/addi/bne glue, so the 16x ratio is not read as a 10-cycle hardware op against a free caller.

## [MINOR] spec-wide — implementability
**Finding.** Every instruction needs an aligned buffer of at least 8 bytes (4 for crcsick.w), and the caller must keep a fallback table loop. The benefit depends on the profiled 16 KiB static buffer.
**Evidence.** CATCH_MISALIGN=1 traps on unaligned lw. The semantics say head and tail bytes stay on the table loop, and the profiled call has none. A call with 7 bytes or an odd pointer gets no speedup. The table and the loop code must stay resident.
**Suggested fix.** Document the alignment and length precondition in the caller contract. No ISA change is needed.

## [MINOR] spec-wide — callability
**Finding.** The gate prices each instruction against the whole software loop (e.g. 29 vs 477 cycles for crc32.f8) but not against the caller loop that still surrounds it, so the per-instruction speedups of 16.4x, 18.8x and 7.8x are overstated.
**Evidence.** The spec's own caller sequence per chunk is lw, lw, xor, instruction, addi, bne, and the rationale puts it at about 25 base cycles. On this core that is lw 5 + lw 5 + xor 3 + addi 3 + taken bne 5 = 21 cycles, and the CRC-64 pair adds a second xor and a second instruction. The gate charges only the 29-cycle instruction. Real cost for crc32.f8 is about 29 + 21 = 50 cycles, so about 9.5x, not 16.4x. For the CRC-64 pair it is about 105 + 24 = 130 cycles against 409, so about 3.1x. Cross-checking the gate's numbers: crc_32 at 7.9% of cycles with 977k software cycles gives a total of about 12.4M cycles. Adding about 22 cycles x 2048 for each of 7 loops is about 315k cycles, or 2.5% of the total. That moves the predicted speedup from 2.19x to about 2.05x, so the verdict holds.
**Suggested fix.** Add the caller-loop cost (about 21-24 cycles per chunk, 4096 x ~13 for crcsick.w) to each instruction's cost in the performance gate or in the spec's own estimate. No design change is needed.

## [MINOR] `crc64.f8h` — callability
**Finding.** The CRC-64 pair runs the full 64-step pass twice per 8-byte chunk and pays the issue overhead twice, so it is the weakest instruction in the set.
**Evidence.** The gate prices f8h at 53 and f8l at 52 cycles, so the pair costs about 105 cycles plus about 25 of caller loop, against 409 cycles of software. That is a real win of about 3x, but the per-instruction 7.8x figure is misleading. The invocation count of 2048 is correct. 98,313 host instructions at 6 per byte is 16,384 bytes, and 16,384 / 8 = 2048. The gate's 'about 578' comes from comparing a 170-instruction C model with a host loop, which is not a data-volume check.
**Suggested fix.** Keep the pair; hidden state between instructions would be worse. State in the spec that the invocations figure is checked against data volume (98,313 / 6 = 16,384 bytes = 2048 chunks), so the UNVERIFIED flag is a false alarm. Optionally note the pair's real net speedup is about 3x.

## [MINOR] spec-wide — callability
**Finding.** The caller contract (4-byte-aligned lw, whole 8-byte chunks, swap16 or bswap32 state conversion) is satisfied only by the profiled 16 KiB static buffer. Short or unaligned calls need head and tail software loops that the spec does not describe in detail.
**Evidence.** CATCH_MISALIGN=1 traps an lw on a pointer that is not word-aligned. The semantics text says only that 'unaligned head bytes and a final partial chunk stay on the table loop'. For the MSB-first CRCs (crcccitt.f8, crc64.f8h/l) the state is kept byte-reversed, so the head loop's natural-order crc must be converted on entry and on exit. With slow shifts that is about 10 instructions, or about 60 cycles, per call. That is negligible at 16384 bytes but dominates for messages under about 64 bytes.
**Suggested fix.** Add one sentence to each MSB-first instruction's semantics giving the exact conversion between the head or tail loop's natural-order crc and the instruction's swapped state (swap16 for crcccitt.f8, bswap32 on both halves for crc64). Say that messages shorter than one chunk plus alignment padding should skip the extension and use the table loop.

## [MINOR] spec-wide — benefit
**Finding.** The per-instruction ratios leave out the caller's loop overhead because marshal_words is 0, so the real ratios are lower than printed, and the real whole-application gain is about 2.1x on the credited loops.
**Evidence.** Each chunk still needs lw, lw, xor, addi and a taken bne, about 21 cycles (13 for crcsick.w). Real ratios are crc32/16/dnp/kermit 477/(29+21) = 9.5x, crcccitt 545/50 = 10.9x, crcsick.w 374/(37+13) = 7.5x. Credited savings are 4 x 7.07 + 8.17 + 4.7 + 10.7 = about 51.8% of cycles, so about 2.07x against the gate's 2.19x and the Amdahl ceiling of 2.48x. No instruction is at or below 1.0. If crc_modbus, crc_xmodem and crc_ccitt_ffff, which run the same instructions, are counted too, another 23% of cycles is saved and the total is about 4x.
**Suggested fix.** Count the loop overhead in the stated ratios (or in marshal_words). Report 2.1x credited and about 4x including siblings, and state that the siblings need the shared static crc_ccitt_generic and the crc_16/crc_modbus bodies to be rewritten.

## [MINOR] `crc64.f8h` — benefit
**Finding.** invocations=2048 is correct, and the gate's '~578 implied' flag is an artifact of comparing host instruction counts, not a defect in the declaration.
**Evidence.** One call processes 16384 bytes (trip_count) and one invocation consumes 8 bytes, which gives 2048. The 98,313 host instructions are 6 per byte x 16384. The model's 170 instructions are a bit-serial reference, not a per-chunk cost. The same holds for f8l, and for crcsick.w (16384/4 = 4096).
**Suggested fix.** Cite trip_count / bytes-per-invocation in the spec. No change to the declared values.

## [MINOR] spec-wide — benefit
**Finding.** Nothing mechanical checks that the chunked caller sequence reproduces libcrc's byte loop, or that the head and tail paths feed the chunk loop correctly.
**Evidence.** Gate 2 and link 1 compare the C models with each other and with the sanitizers, never with libcrc. By GF(2) linearity I checked that the fold (state XORed into the first word, 16-bit CRCs through a 32-bit register, byte-reversed MSB-first results) is correct, but I could not run it. The head and tail code (lw alignment, swap16 conversion for the ccitt state, the packed (p<<16)|crc state for sick after a software head) is not covered by any vector, and the profiled buffer has neither.
**Suggested fix.** Add a differential test of the chunked C loop against the original table loop for all ten functions. Use random buffers with lengths 0-40 and offsets 0-3, including the CRC-SICK and ccitt head and tail hand-offs.

## [MINOR] `crc64.f8l` — benefit
**Finding.** The prose is inconsistent: it says rd may alias rs1/rs2, but the caller sequence requires f8h's rd not to overwrite m0 or m1.
**Evidence.** The semantics section says 'rd may alias rs1/rs2' and also 'rd of the first must not overwrite m0 or m1, which the second still reads'. Hardware reads both sources at issue, so aliasing is legal for a single instruction, but the pair breaks if f8h's rd aliases m0 or m1.
**Suggested fix.** Say that rd aliasing is legal per instruction and that the pair requires a distinct rd for the first instruction. Make the C wrapper use three distinct registers.

## [MINOR] spec-wide — legality
**Finding.** Every instruction has an empty `spike_model`, and the spec-level `encodings`, `tests`, `link1_vectors` and `gate2_vectors` are empty or 0, with `passes_link1` and `passes_gate2` false.
**Evidence.** The spec text says the extension is verified against an independent Spike model, but the artifact has `spike_model: ''` on all eight instructions. Anyone who takes this spec alone cannot reproduce that check or the 100,000-vector run.
**Suggested fix.** Populate `spike_model` for each instruction and record the vector counts once Gate 2 and link 1 have run. I found no legality or completeness defect in the encodings or semantics themselves.

## [MINOR] `crc64.f8h / crc64.f8l` — legality
**Finding.** The pair is only a complete operation if the caller keeps m0 and m1 live and uses distinct destinations. The `rd may alias rs1/rs2` note is true for each instruction alone but unsafe when applied to the pair.
**Evidence.** The semantics already say f8h's rd must not overwrite m0 or m1, which f8l still reads. The generic sentence at the end of each block (`rd may alias rs1/rs2`) invites an unsafe allocation such as `crc64.f8h m0,m0,m1`.
**Suggested fix.** In crc64.f8h's semantics, say outright that `rd` must differ from `rs1` and `rs2` whenever the paired f8l follows. In crc64.f8l, add the corresponding requirement for the register holding the new s0.
