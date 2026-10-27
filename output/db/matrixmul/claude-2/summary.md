# RED claude-2 — matrixmul

- backend: claude: claude-sonnet-5-5 (effort=high) + 8 stage overrides (models.toml)
- result: **converged (A1+A2)**
- A2 rounds: 3
- Gate 0 (workload): PASS 409,133,689 instructions, 98.8% in the project's own code, 0.00% drift
    - matrixmul_workload.c: 409,133,689 instructions, 98.8% in the project's own code
    - drives: multiply, fill, main, call_weak_fn
- Gate 1 (re-profile ±5%): PASS runs 409133761 vs 409133761 cycles (0.00% vs ±5%)
- Gate 2 (C model vs Spike): PASS 3/3 instructions: C model and patched Spike agree on 100,000 random + corner vectors
- Gate 3 (predicted speedup): PASS predicted 1.98x over 68% of cycles (bar 1.32x; relaxed for redesign round 2, priced on the shipped spec)
- Gate 4 (security): PASS 13 checks over 512 adversarial vectors per model: 0 mechanically confirmed blocking, 1 advisory; 3 check(s) could not run
    - (advisory) xfop.lo, xfop.hi: The fixed-latency claim is asserted for a non-normative sequential binary32 datapath that the mechanical timing checks never see, so a data-dependent normalise, round or early-out in the hardware would leak operand values (keys and nonces on these kernels).
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- coverage: 100.00% (80% target)
- spec review: 0 blocking, 24 total (implementability, callability, benefit, legality)
    - [major] xfop.hi: The gate prices xfop.hi at 34 cycles (30 issue + 3 MAC), but the stated datapath needs about 3 x 16-20 = 48-60 cycles of arithmetic, so its latency is real and arithmetic-dominated.
    - [major] spec: The implied area is about 350-400 flops plus a 24-bit alignment shifter, a leading-zero normaliser and a 48-bit adder, which is 3-4x the 'small' bar (under 100 flops) and larger than the core's 160-flop divider.
    - [major] spec: The 1.98x prediction compares ~16.1k cycles of xfop instructions against the full ~59.5k cycles of replaced software, but the caller's remaining loop work is not in the 16.1k. That work is the operand loads, pointer arithmetic, loop branch, and the stores of drained results.
    - [major] spec: The mechanical checks can see only the single-instruction rd output, so nearly all the architectural behaviour is unverified. That covers the accumulator updates, the LA/LB latch, the cross products LA*b1 and a1*LB, the xfop.rd select/clear logic, and sel 7. The spec itself has gate2_vectors 0, link1_vectors 0, tests [] and an empty spike_model.
    - [major] xfop.lo, xfop.hi: The performance gate prices each MAC at 1 cycle, but the spec's own datapath takes 16-20 cycles per multiply-add, so the 1.98x prediction is overstated.
    - [major] spec: The 16.1k-cycle estimate omits all the software the spec keeps (A/B loads, address arithmetic, loop control, C stores, clearMatrix), so the application bound is too high.
- verification edges:
- PASS link1: coverage 100.00% >= 80%; 3/3 C models built and survived 100,000 vectors under ASan+UBSan
- PASS link2: 3/3 instructions: C model and patched Spike agree on 100,000 random + corner vectors
- ISASpec: RED-XFOP: binary32 2x2 outer-product accumulator for RV32IM (3 instructions)
- shape: 3 instruction(s) [2 reg, 1 reg_imm], widest operand block 2 words, declared state 192 bits
  - register file `facc`: 4 x 32 bits — The four binary32 running sums of the 2x2 block of C being formed:
FACC[0] = C00 = C[i][j], FACC[1] = C01 = C[i][j+1], FACC[2] = C10 =
C[i+1][j], FACC[3] = C11 = C[i+1][j+1]. Written by xfop.lo (C00) and
xfop.hi (C01, C10, C11), drained by xfop.rd sel 0..3. These four
registers let a 3-input multiply-accumulate issue as a 2-source R-type
instruction, and let each A and B element the software loads feed two
MACs instead of one.

  - register file `flat`: 2 x 32 bits — The operand latch: FLAT[0] = LA = the a0 and FLAT[1] = LB = the b0 of the
most recent xfop.lo, as raw 32-bit patterns. xfop.hi reads them to form
the two cross products LA*b1 and a1*LB, so one k step needs only two
2-source instructions for four MACs. Readable and clearable through
xfop.rd sel 4, 5 and 7.

  - `xfop.lo` (custom-1, reg, 2x32b, compute): xfop.lo rd, rs1, rs2 is the first half of one k step of a 2x2
outer-product accumulation. x[rs1] = a0 (an element of A's row i) and
x[rs2] = b0 (an element of B's column j), both binary32 bit patterns.
  p   = a0 * b0, rounded to binary32
  C00 = C00 + p, rounded to binary32
  LA  = a0, LB = b0   (raw 32-bit copies, kept for the xfop.hi that follows)
  x[rd] = p
Operand packing (register form): in[0] = x[rs1], in[1] = x[rs2], out[0] ->
x[rd]. out[1] is not architectural and the model writes it 0. Reading x0
gives 0x00000000 (+0.0). Both sources and C00 are read before anything is
written. rs1 = rs2 is allowed. If rd = x0 the product is discarded, but
C00, LA and LB are still written. That is how the kernel issues it.
FACC[1..3] are not touched.

Arithmetic (shared with xfop.hi):
- IEEE 754-2008 binary32 multiplication, then a separate binary32 addition.
  Each is rounded with roundTiesToEven. This is NOT a fused multiply-add:
  the product is rounded to binary32 before it is added, exactly as the C
  expression c + a*b does with float operands.
- Subnormal inputs and outputs are fully supported (gradual underflow, no
  flush-to-zero, no denormals-are-zero). Overflow gives +/-Infinity.
- The sign of a product is the XOR of the operand signs, including zero
  results (-2.0 * +0.0 = -0.0). An exactly-zero sum of opposite-signed
  addends is +0.0, and (-0.0) + (-0.0) = -0.0.
- NaN rule: whenever IEEE 754 would produce a NaN, the result is exactly
  0x7FC00000, the RISC-V canonical quiet NaN. That covers any NaN operand
  (signalling or quiet, any sign or payload), Inf * 0 and Inf + (-Inf).
  It applies to x[rd] and to every value written into FACC. A NaN product
  always makes the sum a NaN, so the canonical sum does not depend on the
  product's NaN payload.
- No exception flags, no fcsr access, no traps.

Timing: a fixed latency, independent of operand values and of the state.
There is no early-out for zero, Inf, NaN or subnormal operands.

Use (see xfop.hi for the whole schedule): xfop.lo x0, A[i][k], B[k][j]
immediately followed by xfop.hi x0, A[i+1][k], B[k][j+1]. On its own,
xfop.lo is also a 1x1 dot-product step, used for the odd row or column of
an odd-sized matrix: K x xfop.lo, then xfop.rd sel 0.

Single-instruction vectors (x[rd] = p, independent of the state):
  0x3FC00000(1.5)  * 0x40000000(2.0)  -> 0x40400000 (3.0)
  0x00000001       * 0x3F000000(0.5)  -> 0x00000000 (2^-150 is a tie; to even, +0)
  0x00000003       * 0x3F000000       -> 0x00000002 (tie 1.5*2^-149 rounds to even)
  0x7F7FFFFF       * 0x40000000       -> 0x7F800000 (overflow to +Inf)
  0x7F800000       * 0x00000000       -> 0x7FC00000 (invalid, canonical NaN)
  0xFFC12345       * 0x3F800000       -> 0x7FC00000 (payload and sign not propagated)
  0x80000000       * 0x3F800000       -> 0x80000000 (-0.0)
Unfused rounding, from a clean FACC (this distinguishes it from an FMA):
  xfop.lo x0, 0xBF800002, 0x3F800000  -> C00 = 0xBF800002
  xfop.lo x0, 0x3F800001, 0x3F800001  -> p = 0x3F800002, C00 = 0x00000000
                                         (an FMA would give 0x28800000)

  - `xfop.hi` (custom-1, reg, 2x32b, compute): xfop.hi rd, rs1, rs2 completes one k step of a 2x2 outer-product
accumulation, using the latch that xfop.lo left. x[rs1] = a1 (the element
of A's row i+1) and x[rs2] = b1 (the element of B's column j+1).
  C01 = C01 + (LA * b1)     i.e. C[i][j+1]   += A[i][k]   * B[k][j+1]
  C10 = C10 + (a1 * LB)     i.e. C[i+1][j]   += A[i+1][k] * B[k][j]
  C11 = C11 + (a1 * b1)     i.e. C[i+1][j+1] += A[i+1][k] * B[k][j+1]
  x[rd] = a1 * b1
Each product is rounded to binary32, then each sum is rounded to binary32,
with exactly the arithmetic of xfop.lo: RNE, unfused, gradual underflow,
IEEE signed zeros, every NaN result becomes 0x7FC00000, no flags or traps.
Operand packing (register form): in[0] = x[rs1], in[1] = x[rs2], out[0] ->
x[rd]. out[1] is not architectural and the model writes it 0. Reading x0
gives +0.0. All three products use the values of LA, LB, C01, C10 and C11
from before the instruction. The three accumulator updates are
independent of each other. xfop.hi reads FLAT but never writes it, and it
never touches C00. LA and LB are whatever the most recent xfop.lo wrote,
or 0x00000000 after reset or xfop.rd sel 7. If rd = x0 the returned
product is discarded and FACC is still updated.

Timing: a fixed latency (three multiply-adds on the unit's datapath),
independent of operand values and of the state. No early-out.

Kernel schedule for C = A x B (M x K times K x N, C cleared to +0.0 by
clearMatrix, as the application does). It is bit-identical to the
original ijk loop, because each C element receives the same rounded
products, added in the same k order, starting from +0.0:
  xfop.rd x0, x0, 7                         // entry: clear FACC and FLAT
  for each full 2x2 block (i, j), i < M&~1, j < N&~1, step 2:
    for k = 1..K:
      xfop.lo x0, A[i][k],   B[k][j]
      xfop.hi x0, A[i+1][k], B[k][j+1]
    C[i][j]   = xfop.rd sel 0;  C[i][j+1]   = xfop.rd sel 1
    C[i+1][j] = xfop.rd sel 2;  C[i+1][j+1] = xfop.rd sel 3
  for each remaining element (odd last row / column):
    for k = 1..K: xfop.lo x0, A[i][k], B[k][j]
    C[i][j] = xfop.rd sel 0
  xfop.rd x0, x0, 7                         // exit: scrub
xfop.lo and xfop.hi of one k step are issued back to back. Nothing on this
core can come between them (no interrupts; the loads and address
arithmetic in the loop cannot trap).

Worked sequence, from reset or after xfop.rd x0, x0, 7. A rows i, i+1 =
[1.5, 1.0], [0.25, 2.0] (k = 1, 2); B rows k = 1, 2 = [2.0, -4.0],
[0.5, 3.0] (columns j, j+1):
  xfop.lo t0, 0x3FC00000(1.5),  0x40000000(2.0)  -> t0 = 0x40400000 (3.0)
       C00 = 0x40400000, LA = 0x3FC00000, LB = 0x40000000
  xfop.hi t0, 0x3E800000(0.25), 0xC0800000(-4.0) -> t0 = 0xBF800000 (-1.0)
       C01 = 0xC0C00000 (-6.0), C10 = 0x3F000000 (0.5), C11 = 0xBF800000 (-1.0)
  xfop.lo t0, 0x3F800000(1.0),  0x3F000000(0.5)  -> t0 = 0x3F000000 (0.5)
       C00 = 0x40600000 (3.5), LA = 0x3F800000, LB = 0x3F000000
  xfop.hi t0, 0x40000000(2.0),  0x40400000(3.0)  -> t0 = 0x40C00000 (6.0)
       C01 = 0xC0400000 (-3.0), C10 = 0x3FC00000 (1.5), C11 = 0x40A00000 (5.0)
  xfop.rd sel 0..3 -> 0x40600000, 0xC0400000, 0x3FC00000, 0x40A00000
Special values, from a clean state:
  xfop.lo x0, 0x7F800000(+Inf), 0x3F800000      -> C00 = 0x7F800000, LA = +Inf, LB = 1.0
  xfop.hi t1, 0x00000000(+0),   0xFF800000(-Inf) -> t1 = 0x7FC00000 (0 * -Inf)
       C01 = 0xFF800000 (+Inf * -Inf), C10 = 0x00000000 (+0 + +0*1.0), C11 = 0x7FC00000
Single-instruction vectors with LA = LB = 0 (x[rd] = a1*b1 never depends
on the state):
  0x00000003 * 0x3F000000 -> 0x00000002
  0x7F7FFFFF * 0x40000000 -> 0x7F800000
  0x7F800000 * 0x00000000 -> 0x7FC00000
  0xFFC12345 * 0x3F800000 -> 0x7FC00000
  0x80000000 * 0x3F800000 -> 0x80000000

  - `xfop.rd` (custom-1, reg_imm, 2x32b, move): xfop.rd rd, rs1, sel moves one element of the unit's state into x[rd] and
clears it. It is an I-type instruction: sel = imm[2:0], taken from
instruction bits 22:20. imm[11:3] (bits 31:23) are ignored and assemblers
write them 0, which is why funct7 is listed as 0000000. The decoder
matches opcode 0101011 with funct3 = 001 only, so the instruction occupies
the whole custom-1 funct3=001 slot. x[rs1] is not used (write x0).
  sel 0..3: x[rd] = FACC[sel] (C00, C01, C10, C11), then FACC[sel] = 0x00000000
  sel 4:    x[rd] = LA, then LA = 0x00000000
  sel 5:    x[rd] = LB, then LB = 0x00000000
  sel 6:    x[rd] = 0x00000000, no state change (reserved)
  sel 7:    x[rd] = 0x00000000, and all four FACC and both FLAT
            elements become 0x00000000 (clear all)
The value moved is the raw 32-bit pattern, with no arithmetic. Because
FACC only ever holds +0.0 or a canonical sum (see state_note), the value
read from FACC is exactly the C element the original loop computes.
Operand packing (reg_imm form): in[0] = x[rs1] (ignored), in[1] = the
zero-extended immediate (the model uses in[1] & 7), out[0] -> x[rd]. out[1]
is not architectural and the model writes it 0. If rd = x0 the value is
discarded and the element is still cleared. That is how the entry/exit
scrub xfop.rd x0, x0, 7 is issued.
Timing: fixed (a 6-way select and a clear), independent of sel and of
the state.
Hardware note: PicoRV32 offers an I-type custom instruction on PCPI like
any other, with pcpi_rs2 = x[insn[24:20]]. That value is meaningless here
and the unit must take sel from pcpi_insn[22:20].

Gate 2 tests each instruction alone from reset, and alone this one only
ever sees a zero state, so it always returns 0. Its data path is checked
by the sequences: in the xfop.hi worked sequence, sel 0..3 return
0x40600000, 0xC0400000, 0x3FC00000, 0x40A00000. A second read of the
same sel then returns 0x00000000. After xfop.lo x0, 0x3FC00000,
0x40000000, sel 4 returns 0x3FC00000 and sel 5 returns 0x40000000. After
sel 7, every sel returns 0.

