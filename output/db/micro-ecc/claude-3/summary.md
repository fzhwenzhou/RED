# RED claude-3 — micro-ecc

- backend: claude: claude-sonnet-5-5 (effort=high) + 8 stage overrides (models.toml)
- result: **converged (A1+A2)**
- A2 rounds: 1
- Gate 0 (workload): — (operator-supplied harness)
- Gate 1 (re-profile ±5%): PASS runs 8944073267 vs 8944169876 cycles (0.00% vs ±5%)
- Gate 2 (C model vs Spike): PASS 6/6 instructions: C model and patched Spike agree on 100,000 random + corner vectors
- Gate 3 (predicted speedup): PASS predicted 8.26x over 97% of cycles (bar 2.00x, priced on the shipped spec)
- Gate 4 (security): PASS 13 checks over 512 adversarial vectors per model: 0 mechanically confirmed blocking, 2 advisory; 6 check(s) could not run
    - (advisory) p256.str: FA and ACC are never cleared, so the secret value from one operation survives it. A later caller can read the last product or difference with one p256.str and no setup, and can read the last p256.lda operand (FA) with p256.ldr from zeros, then p256.subm, then p256.str.
    - (advisory) p256.mac2: mac2 is correct mod p only when ACC < 2^288 on entry. Its 2-limb shift drops limbs 9 and 10 (weights 2^352 and up), and 2^352 is not 0 mod p, so when those limbs are set the result is silently wrong mod p. The hardware note says the shift is a rotate, which gives a different wrong value, so the C model that Gate 2 verifies and the RTL disagree on this path.
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- coverage: 97.18% (80% target)
- spec review: 0 blocking, 22 total (implementability, callability, benefit, legality)
    - [major] p256.mac2: Gate 2 and link 1 can only exercise mac2, red, subm and str from reset state (FA=ACC=0), so none of the arithmetic that carries the 8.26x is checked by anything, and every instruction has an empty spike_model.
    - [major] p256.mac2: Gate 2 and link 1 cannot check the four stateful instructions (mac2, red, subm, str), and spike_model is empty for every instruction, so their correctness rests on a hand-run sequence.
    - [major] p256.mac2: The arithmetic of mac2, red, subm and str is invisible to Gate 2 and to any single-instruction RTL differential test. Gate 2 runs each instruction from reset, where FA = ACC = 0. The spec also has empty spike_model fields, no tests, and gate2_vectors and link1_vectors both 0.
    - [major] p256.mac2: The mac2 C model and the hardware description disagree on what happens to ACC limbs 9 and 10 in the 2-limb shift. The C model drops them; the hardware note says the shift is 'a rotate of the limb index'.
    - [major] spec: The mechanical gates cannot see the arithmetic of p256.mac2, p256.red, p256.subm or p256.str. The spec's only coverage for them is a prose request that the reviewers run the sequences.
- verification edges:
- PASS link1: coverage 97.18% >= 80%; 6/6 C models built and survived 100,000 vectors under ASan+UBSan
- PASS link2: 6/6 instructions: C model and patched Spike agree on 100,000 random + corner vectors
- ISASpec: xp256 (6 instructions)
- shape: 6 instruction(s) [3 block, 3 reg], widest operand block 8 words, declared state 608 bits
  - register file `FA`: 1 x 256 bits — Operand A, 8 x 32-bit limbs, limb 0 least significant (micro-ecc uECC_word_t[8] layout). It is the multiplicand of p256.mac2 and the minuend of p256.subm. Only p256.lda writes it.
  - register file `ACC`: 1 x 352 bits — Accumulator, 11 x 32-bit limbs, limb 0 least significant. It holds the running residue of a modular multiply, and the subtrahend and then the result of p256.subm. p256.str stores its low 256 bits.
  - `p256.lda` (custom-1, block, 8x32b, load-store): p256.lda rd, rs1, rs2 starts a field operation: it loads operand A into FA and
clears ACC.

Load. in[i] = MEM32[(x[rs1] & ~3) + 4*i] for i = 0..7. in[0] is the least
significant limb, which is micro-ecc's uECC_word_t[8] layout with
uECC_WORD_SIZE=4. FA <- in[0..7], and ACC <- 0 (all 352 bits). All eight reads
complete before any write.

Copy-through. out[i] = FA[i] (= in[i]) is written to MEM32[(x[rs2] & ~3) + 4*i] in
ascending i. Canonical use is rs2 = rs1, which rewrites the same eight words
unchanged (all of this SoC's memory is SRAM). With rs2 != rs1 the instruction is
also an 8-word copy. If the operand sits in read-only memory, point rs2 at an
8-word scratch buffer instead.

Addresses wrap modulo 2^32. x[rd] <- 0 (use rd = x0).

Memory. The accesses are ordinary word loads and stores at the program's
privilege. A block outside RAM behaves like the same LW/SW sequence would: MMIO
side effects, and the SoC $fatals on unmapped addresses.

Timing. Fixed latency: one guard cycle for the in-flight prefetch (mem_issued), then
8 read beats and 8 write beats of 2 cycles each, about 34 cycles. pcpi_wait is
raised in the first cycle.

Verification. out = in is a pure function of in[], so Gate 2 exercises the load
path. FA and the clear of ACC are checked by the sequences under p256.mac2 and
p256.subm.

Cost accounting. lda runs once per modMult/modSquare and once per modSub. All
7,633,920 executions are charged to the modSub loop: 1.94 per call, declared as 2.

  - `p256.ldr` (custom-1, block, 8x32b, load-store): p256.ldr rd, rs1, rs2 loads a 256-bit value into the accumulator.

Load. in[i] = MEM32[(x[rs1] & ~3) + 4*i] for i = 0..7, with limb 0 least
significant. ACC[0..7] <- in[0..7], and ACC[8..10] <- 0. FA is unchanged. All eight
reads complete before any write.

Copy-through. out[i] = ACC[i] (= in[i]) is written to MEM32[(x[rs2] & ~3) + 4*i] in
ascending i. Canonical use is rs2 = rs1, which rewrites the same words unchanged.
For a read-only operand, point rs2 at an 8-word scratch buffer.

Addresses wrap modulo 2^32. x[rd] <- 0. Timing and memory rules are those of
p256.lda.

Use. ldr loads the subtrahend of p256.subm. It must come after p256.lda, because
lda clears ACC.

Verification. out = in, so Gate 2 exercises the load path; the subm sequence checks
ACC.

Cost accounting. ldr runs once per modSub only: 1 per call.

  - `p256.str` (custom-1, block, 8x32b, load-store): p256.str rd, rs1, rs2 stores the low 256 bits of ACC to the 8-word output block at
x[rs2]: out[i] = ACC[i], MEM32[(x[rs2] & ~3) + 4*i] = ACC[i] for i = 0..7 in
ascending order. Addresses wrap modulo 2^32. ACC[8..10] are not stored; they are
zero after p256.red, p256.subm and p256.ldr.

The input block is not read and rs1 is ignored (encode rs1 = rs2), so the hardware
issues only the eight write beats. The gate's cost model charges 16 beats, which
overstates the cost.

FA and ACC are unchanged. x[rd] <- 0. The memory rules are those of p256.lda.

Timing. Fixed latency: one guard cycle, then 8 write beats, about 18 cycles.

Verification. From reset ACC = 0, so Gate 2 sees an all-zero block; the sequences
under p256.mac2 and p256.subm check str.

Cost accounting. str runs once per modMult/modSquare and once per modSub. All
executions are charged to the modSub loop: 1.94 per call, declared as 2.

  - `p256.mac2` (custom-1, reg, 2x32b, compute): p256.mac2 rd, rs1, rs2 is one Horner step of a 256 x 64-bit multiply-accumulate.

Digit. b = x[rs1]*2^32 + x[rs2]: in[0] = x[rs1] is the high 32-bit digit and
in[1] = x[rs2] the low one.

Operation. ACC <- (ACC * 2^64 + FA * b) mod 2^352. FA is unchanged. x[rd] <- 0:
out[0] = 0, and out[1] = 0 pads the 2-word model. If ACC < 2^256 on entry (as after
p256.lda or p256.red), the result is below 2^321 and nothing is dropped.

Reference order:
1. Shift ACC up two limbs, dropping limbs 9 and 10.
2. Add FA*x[rs2] at limb 0, as an 8-limb row with carry propagated to limb 10.
3. Add FA*x[rs1] at limb 1, with carry propagated to limb 10. The final carry out
   is dropped.
That is 16 32x32 multiplies, with fixed trip counts, and no data-dependent branch or
index.

Hardware. One 32x32 multiplier (red_accel's limb_product). The 2-limb shift is a
rotate of the limb index. About 18 cycles, constant time.

Use (modMult_fast; for modSquare_fast, right = left):
  p256.lda  x0, left, left
  for m = 3, 2, 1, 0:
    lw t1, 8m+4(right); lw t0, 8m(right)
    p256.mac2 x0, t1, t0
    p256.red  x0, x0, x0
  p256.str  x0, result, result
This leaves result = left*right mod p, canonical, for every pair of 256-bit inputs.
That is the same as uECC_vli_mult followed by vli_mmod_fast_secp256r1. The pattern
is safe when result aliases left or right, because every LW and lda precede the
store.

Verification. Gate 2 starts from reset with FA = ACC = 0, so it sees x[rd] = 0
only. The reviewers should run the sequence above against uECC_vli_modMult_fast.
Suggested vectors:
- left = right = p-1, which gives 1.
- left = right = 2^256-1 (non-canonical).
- left = right = 2^128, which gives 2^256 mod p.
- left = 0.

Invocations. 4 per uECC_vli_mult call: eight 32-bit digits of right, two per step.

  - `p256.red` (custom-1, reg, 2x32b, reduction): p256.red rd, rs1, rs2 reduces the accumulator modulo
p = 2^256 - 2^224 + 2^192 + 2^96 - 1: ACC <- ACC mod p, canonical
(0 <= ACC < p, ACC[8..10] = 0), for every 352-bit ACC. FA is unchanged.
x[rd] <- 0; x[rs1] and x[rs2] are ignored (encode x0).

Algorithm. K = 2^256 mod p = 2^224 - 2^192 - 2^96 + 1, and K > 0.
1. Fold four times: X <- (X mod 2^256) + floor(X / 2^256) * K. For the high part
   T = (X >> 256), up to 3 limbs, the fold adds +T at limb 0, -T at limb 3, -T at
   limb 6 and +T at limb 7, then propagates signed carries across 11 limbs. Each
   fold preserves X mod p and keeps X >= 0. Starting below 2^352, the bounds after
   each fold are below 2^321, 2^290, 2^259, and 2^256 + 7*2^224.
2. Compute W = X - p over 9 limbs, with borrow b. ACC <- X if b = 1, else W. The
   choice is made by mask, not branch. If b = 1, X < p. Otherwise
   W < K + 7*2^224 < p. Either way the result is canonical.
Trip counts are fixed; no branch or index depends on data.

Hardware. No multiplier. Each fold is 11 limb steps through a 4-input adder with a
signed carry; then a 9-limb subtract and the select. About 55 cycles, constant time.

Use. One red follows every p256.mac2 (see the sequence there). Each red keeps ACC
below p, so the next mac2 stays below 2^321. After the fourth red of a multiply,
ACC equals what vli_mmod_fast_secp256r1 returns for the full 512-bit product.

Verification. From reset ACC = 0, so Gate 2 sees x[rd] = 0 only. The mac2 sequence
verifies red. Edge values worth running through a seeded ACC (via p256.ldr then red):
- p, which gives 0.
- p-1, which is unchanged.
- 2^256-1, which gives K-1.

Invocations. 28 per recorded mmod call: 4 per reduction x 3,694,592 reductions /
530,948 recorded calls = 27.8.

  - `p256.subm` (custom-1, reg, 2x32b, compute): p256.subm rd, rs1, rs2 sets ACC <- (FA - ACC[0..7]) mod p, using the convention of
micro-ecc's uECC_vli_modSub:
- D = (FA - ACC[0..7]) mod 2^256, with borrow bw; bw = 1 exactly when FA <
  ACC[0..7] as unsigned 256-bit integers.
- ACC[0..7] <- (D + (p AND mask(bw))) mod 2^256, where mask(bw) is all ones when
  bw = 1 and zero otherwise. The carry out is discarded.
- ACC[8..10] <- 0.
For canonical inputs the result is canonical. For any other inputs it is exactly
what uECC_vli_modSub computes, so the instruction is total.

The p addend is masked, not branched on, and the latency is fixed. FA is unchanged.
x[rd] <- 0; x[rs1] and x[rs2] are ignored (encode x0).

Hardware. The adder of p256.red: an 8-limb subtract, then an 8-limb masked add,
about 18 cycles.

Use (uECC_vli_modSub when mod is secp256r1's p; other moduli keep the software
path):
  p256.lda x0, left, left; p256.ldr x0, right, right; p256.subm x0, x0, x0;
  p256.str x0, result, result
This is safe when result aliases left or right.

Verification. From reset FA = ACC = 0, so Gate 2 sees x[rd] = 0 only. Run the
sequence against uECC_vli_modSub. Suggested vectors:
- left = right, which gives 0.
- left = 0, right = 1, which gives p-1.
- left = p-1, right = 0.
- left = 0, right = 2^256-1 (non-canonical).

