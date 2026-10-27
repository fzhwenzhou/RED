# RED claude-2 — libcrc

- backend: claude: claude-sonnet-5-5 (effort=high) + 8 stage overrides (models.toml)
- result: **converged (A1+A2)**
- A2 rounds: 1
- Gate 0 (workload): PASS 364,004,779 instructions, 100.0% in the project's own code, 0.00% drift
    - libcrc_workload.c: 364,004,779 instructions, 100.0% in the project's own code
    - drives: crc_sick, crc_ccitt_1d0f, crc_ccitt_ffff, crc_xmodem, crc_dnp, crc_kermit
- Gate 1 (re-profile ±5%): PASS runs 364004851 vs 364004851 cycles (0.00% vs ±5%)
- Gate 2 (C model vs Spike): PASS 8/8 instructions: C model and patched Spike agree on 100,000 random + corner vectors
- Gate 3 (predicted speedup): PASS predicted 2.19x over 60% of cycles (bar 1.86x; Amdahl caps this workload at 2.48x for the 59.6% it covers, so the bar is 75% of that rather than the 2.00x default, priced on the shipped spec)
- Gate 4 (security): PASS 13 checks over 512 adversarial vectors per model: 0 mechanically confirmed blocking, 0 advisory; 8 check(s) could not run
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
    - NOT CHECKED — timing.secret_branch: memcheck could not run — valgrind:  Fatal error at startup: a function redirection
- coverage: 85.53% (80% target)
- spec review: 0 blocking, 16 total (implementability, callability, benefit, legality)
    - [major] crc64.f8h / crc64.f8l: The pair is credited against the same crc_64_ecma software cost twice, so the loop's speedup is overstated and the prediction leans on it.
    - [major] spec: The spec never states the area of the shared engine, and the 64-bit width needed by CRC-64 sets it for all eight instructions.
    - [major] crc64.f8h: The CRC-64 pair is worth about 3x on a 6.8% loop, not the 7.8x the gate prints, and it is the only reason the shared engine has to be 64 bits wide.
    - [major] spec: The shared engine's area is never quantified, and the 8-steps-per-cycle unroll is a large cost for a cycle saving the pricing barely sees.
- verification edges:
- PASS link1: coverage 85.53% >= 80%; 8/8 C models built and survived 100,000 vectors under ASan+UBSan
- PASS link2: 8/8 instructions: C model and patched Spike agree on 100,000 random + corner vectors
- ISASpec: RED-Xcrcfold (8 instructions)
- shape: 8 instruction(s) [8 reg], widest operand block 2 words, declared state 0 bits
  - `crc32.f8` (custom-1, reg, 2x32b, compute): Reflected (LSB-first) CRC-32 with polynomial 0xEDB88320, over 8 message
bytes, starting from an all-zero register. This is libcrc's CRC_POLY_32,
the polynomial behind crc_tab32.
Operands: in[0] = x[rs1] holds message bytes M0..M3 and in[1] = x[rs2] holds
M4..M7, with Mi in bits 8i+7..8i of its register. That is exactly what two lw
of an 8-byte chunk produce on this little-endian core.
Result: out[0] -> x[rd] is the 32-bit register after all 8 bytes. out[1] is
always 0 and not architecturally visible.
Bytes are consumed in the order M0..M7, each LSB first.
The result is the same as 8 iterations of libcrc's
crc = (crc >> 8) ^ crc_tab32[(crc ^ byte) & 0xFF] from crc = 0.
Caller sequence: by GF(2) linearity, starting from state S is the same as
starting from 0 with S XORed into M0..M3, S bits 7..0 into M0. So the
running CRC s lives in a register and each aligned chunk costs:
lw w0,0(p); lw w1,4(p); xor w0,w0,s; crc32.f8 s,w0,w1; addi p,p,8.
crc_32 starts with s = 0xFFFFFFFF and returns s ^ 0xFFFFFFFF.
Bytes before the first 4-byte-aligned address, and a final partial chunk,
stay on the existing table loop. The profiled 16384-byte buffer has neither.
invocations: one profiled crc_32 call processes 16384 bytes (HotLoopReport
trip_count; 114,696 host instructions per call = 7 per byte). One invocation
consumes 8 bytes, so 16384 / 8 = 2048 per call. Gate 3 will imply only a few
hundred, because the model does 8 shift/XOR steps per byte where the
software does one table lookup per byte. That ratio is biased; the byte
count above is the actual figure.
No memory access, no state, no traps. Latency does not depend on operand
values. rd may alias rs1/rs2, and rd = x0 discards the result.

  - `crc16.f8` (custom-1, reg, 2x32b, compute): Reflected (LSB-first) 16-bit CRC with polynomial 0xA001, over 8 message
bytes, starting from an all-zero register. This is libcrc's CRC_POLY_16, the
crc_tab16 polynomial shared by crc_16 and crc_modbus.
Operands: in[0] = x[rs1] holds message bytes M0..M3 and in[1] = x[rs2] holds
M4..M7, with Mi in bits 8i+7..8i of its register. That is what two lw of an
8-byte chunk produce.
Result: out[0] -> x[rd] is the 16-bit register, zero-extended, so bits
31..16 are always 0. out[1] is always 0 and not architecturally visible.
Bytes are consumed in order M0..M7, each LSB first.
The result is the same as 8 iterations of
crc = (crc >> 8) ^ crc_tab16[(crc ^ byte) & 0xFF] from crc = 0.
Caller sequence: by GF(2) linearity, starting from state S is the same as
starting from 0 with S XORed into M0..M1 (S bits 7..0 into M0). Per aligned
chunk: lw w0,0(p); lw w1,4(p); xor w0,w0,s; crc16.f8 s,w0,w1; addi p,p,8.
crc_16 starts with s = 0x0000 and crc_modbus with s = 0xFFFF. Both use this
instruction unchanged; Gate 3 credits it to crc_16 only.
Unaligned head bytes and a final partial chunk stay on the table loop.
invocations: one profiled crc_16 call processes 16384 bytes (trip_count;
114,704 host instructions per call = 7 per byte). That is 16384 / 8 = 2048
invocations. A ratio-based estimate reads lower because the model does 8
bit steps per byte against one table lookup.
No memory access, no state, no traps. Latency does not depend on operand
values. rd may alias rs1/rs2, and rd = x0 discards the result.

  - `crcdnp.f8` (custom-1, reg, 2x32b, compute): Reflected (LSB-first) 16-bit CRC with polynomial 0xA6BC, over 8 message
bytes, starting from an all-zero register. This is libcrc's CRC_POLY_DNP,
the crc_tabdnp polynomial.
Operands: in[0] = x[rs1] holds message bytes M0..M3 and in[1] = x[rs2] holds
M4..M7, with Mi in bits 8i+7..8i.
Result: out[0] -> x[rd] is the 16-bit register, zero-extended. out[1] is
always 0.
Bytes are consumed in order M0..M7, each LSB first.
The result is the same as 8 iterations of
crc = (crc >> 8) ^ crc_tabdnp[(crc ^ byte) & 0xFF] from crc = 0.
Caller sequence: identical to crc16.f8. s = 0x0000 (CRC_START_DNP). Per
aligned chunk: lw w0; lw w1; xor w0,w0,s; crcdnp.f8 s,w0,w1. crc_dnp's
epilogue (~crc, then byte swap) stays in C.
Unaligned head bytes and a final partial chunk stay on the table loop.
invocations: one profiled crc_dnp call processes 16384 bytes (trip_count;
114,704 host instructions per call). At 8 bytes per invocation that is 2048.
No memory access, no state, no traps. Latency does not depend on operand
values. rd may alias rs1/rs2, and rd = x0 discards the result.

  - `crckermit.f8` (custom-1, reg, 2x32b, compute): Reflected (LSB-first) 16-bit CRC with polynomial 0x8408, over 8 message
bytes, starting from an all-zero register. This is libcrc's
CRC_POLY_KERMIT, the crckrmit.c crc_tab polynomial.
Operands: in[0] = x[rs1] holds message bytes M0..M3 and in[1] = x[rs2] holds
M4..M7, with Mi in bits 8i+7..8i.
Result: out[0] -> x[rd] is the 16-bit register, zero-extended. out[1] is
always 0.
Bytes are consumed in order M0..M7, each LSB first.
The result is the same as 8 iterations of
crc = (crc >> 8) ^ crc_tab[(crc ^ byte) & 0xFF] from crc = 0.
Caller sequence: identical to crc16.f8, with s = 0x0000
(CRC_START_KERMIT). crc_kermit's byte-swap epilogue stays in C.
Unaligned head bytes and a final partial chunk stay on the table loop.
invocations: one profiled crc_kermit call processes 16384 bytes (trip_count;
114,703 host instructions per call). At 8 bytes per invocation that is 2048.
No memory access, no state, no traps. Latency does not depend on operand
values. rd may alias rs1/rs2, and rd = x0 discards the result.

  - `crcccitt.f8` (custom-1, reg, 2x32b, compute): Non-reflected (MSB-first) 16-bit CRC with polynomial 0x1021, over 8 message
bytes, starting from an all-zero register. This is libcrc's CRC_POLY_CCITT,
the crc_tabccitt polynomial of crc_ccitt_generic, used by crc_xmodem,
crc_ccitt_1d0f and crc_ccitt_ffff.
Operands: in[0] = x[rs1] holds message bytes M0..M3 and in[1] = x[rs2] holds
M4..M7, with Mi in bits 8i+7..8i of its register. That is what two lw of an
8-byte chunk produce.
Bytes are consumed in order M0..M7, each MSB first.
Result: out[0] -> x[rd] is the final 16-bit register R in message byte order,
zero-extended: x[rd][7:0] = R[15:8], x[rd][15:8] = R[7:0], x[rd][31:16] = 0.
out[1] is always 0. R equals 8 iterations of libcrc's
crc = (crc << 8) ^ crc_tabccitt[((crc >> 8) ^ byte) & 0xFF] from crc = 0.
Why byte-reversed: in an MSB-first CRC the register's high byte meets the
next message byte. In this order the result XORs straight into the next
lw-loaded word, so the loop needs no byte swap. RV32IM has no rev8, so a
swap would cost about 10 instructions with slow shifts.
Caller sequence: keep s = swap16(crc), i.e. (crc >> 8) | ((crc & 0xFF) << 8).
Per aligned chunk: lw w0,0(p); lw w1,4(p); xor w0,w0,s; crcccitt.f8 s,w0,w1;
addi p,p,8. At entry, s = swap16(start): 0x0000 for xmodem, 0x0F1D for
start 0x1D0F, 0xFFFF for start 0xFFFF. At exit, crc = swap16(s), done once
per call. The instruction is credited to crc_ccitt_1d0f; crc_xmodem and
crc_ccitt_ffff use it identically.
Unaligned head bytes and a final partial chunk stay on the table loop.
invocations: one profiled call processes 16384 bytes (trip_count; 131,085
host instructions per call = 8 per byte). At 8 bytes per invocation that is
2048.
No memory access, no state, no traps. Latency does not depend on operand
values. rd may alias rs1/rs2, and rd = x0 discards the result.

  - `crc64.f8h` (custom-1, reg, 2x32b, compute): High half of a non-reflected (MSB-first) 64-bit CRC with polynomial
0x42F0E1EBA9EA3693, over 8 message bytes, starting from an all-zero
register. This is libcrc's CRC_POLY_64, the crc_tab64 polynomial of
crc_64_ecma and crc_64_we.
Operands: in[0] = x[rs1] holds message bytes M0..M3 and in[1] = x[rs2] holds
M4..M7, with Mi in bits 8i+7..8i of its register.
Bytes are consumed in order M0..M7, each MSB first.
Result: out[0] -> x[rd] is register bits 63..32 in message byte order:
x[rd][7:0] = R[63:56], x[rd][15:8] = R[55:48], x[rd][23:16] = R[47:40],
x[rd][31:24] = R[39:32]. out[1] is always 0. R equals 8 iterations of
crc = (crc << 8) ^ crc_tab64[((crc >> 56) ^ byte) & 0xFF] from crc = 0.
Companion: crc64.f8l, given the same two operands, returns the low half in
the same byte order. One rd cannot hold 64 bits, so the pair is the
complete operation, and each half recomputes the full pass. The halves come
back in exactly the byte order that XORs into the next chunk's two lw words.
Caller sequence: keep s0 = bswap32(crc >> 32) and s1 = bswap32(crc & 0xFFFFFFFF).
Per aligned chunk:
  lw w0,0(p); lw w1,4(p); xor m0,w0,s0; xor m1,w1,s1;
  crc64.f8h s0,m0,m1; crc64.f8l s1,m0,m1; addi p,p,8
Note that rd of the first must not overwrite m0 or m1, which the second
still reads.
crc_64_ecma starts with s0 = s1 = 0. crc_64_we starts with
s0 = s1 = 0xFFFFFFFF and also uses the pair, uncredited. At exit,
crc = bswap32(s0) << 32 | bswap32(s1), done once per call.
Unaligned head bytes and a final partial chunk stay on the table loop.
invocations: one profiled crc_64_ecma call processes 16384 bytes (trip_count;
98,313 host instructions per call = 6 per byte on the 64-bit host). Each
chunk of 8 bytes runs this instruction once, so 2048 per call. crc64.f8l
also runs 2048 times.
No memory access, no state, no traps. Latency does not depend on operand
values. rd may alias rs1/rs2, and rd = x0 discards the result.

  - `crc64.f8l` (custom-1, reg, 2x32b, compute): Low half of the same computation as crc64.f8h: a non-reflected (MSB-first)
64-bit CRC with polynomial 0x42F0E1EBA9EA3693, over 8 message bytes,
starting from an all-zero register.
Operands: in[0] = x[rs1] holds message bytes M0..M3 and in[1] = x[rs2] holds
M4..M7, with Mi in bits 8i+7..8i.
Bytes are consumed in order M0..M7, each MSB first.
Result: out[0] -> x[rd] is register bits 31..0 in message byte order:
x[rd][7:0] = R[31:24], x[rd][15:8] = R[23:16], x[rd][23:16] = R[15:8],
x[rd][31:24] = R[7:0]. out[1] is always 0.
Caller sequence: see crc64.f8h. Both halves take the same pre-XORed operands
m0 = w0 ^ s0 and m1 = w1 ^ s1, so f8h's rd must not overwrite m0 or m1
before f8l reads them. crc64.f8l s1,m0,m1 produces the new s1.
invocations: 16384 bytes per profiled crc_64_ecma call and 8 bytes per
invocation give 2048. It runs alongside crc64.f8h, so the pair is 4096
instruction executions per call, and Gate 3 charges both.
No memory access, no state, no traps. Latency does not depend on operand
values. rd may alias rs1/rs2, and rd = x0 discards the result.

  - `crcsick.w` (custom-1, reg, 2x32b, compute): Four steps of libcrc's CRC-SICK byte recurrence (polynomial 0x8005). Each
step does one 1-bit register shift with a conditional polynomial XOR, then
XORs in the current byte and the previous byte.
CRC-SICK is not a byte-aligned CRC. The register advances one bit per byte,
so the state is never consumed by the message and cannot be folded into
it. The state therefore travels in a register.
Operands: in[0] = x[rs1] is the packed state. Bits 15:0 are crc, bits 23:16
are p (the previous message byte, i.e. libcrc's short_p >> 8), and bits
31:24 are ignored. in[1] = x[rs2] holds message bytes M0..M3, with Mi in bits
8i+7..8i (what an lw of a 4-byte chunk produces).
For i = 0..3 in order:
  1. If bit 15 of crc is 1, crc = ((crc << 1) AND 0xFFFF) XOR 0x8005.
     Otherwise crc = (crc << 1) AND 0xFFFF. The test uses crc before the
     shift.
  2. crc = crc XOR (Mi | (p << 8)).
  3. p = Mi.
Result: out[0] -> x[rd] = (p << 16) | crc, with bits 31:24 = 0. This is the
same packing as rs1, so the result feeds straight back as the next rs1.
out[1] is always 0.
The four steps equal 4 iterations of crc_sick's loop body with
short_p = p << 8.
Caller sequence: s = 0, from CRC_START_SICK = 0 and short_p = 0. Per aligned
word: lw w,0(p); crcsick.w s,s,w; addi p,p,4. At exit, crc = s & 0xFFFF,
then crc_sick's byte-swap epilogue in C. If head bytes ran in software,
pack s = (prev_byte << 16) | crc before the first instruction.
invocations: one profiled crc_sick call processes 16384 bytes (trip_count;
180,233 host instructions per call = 11 per byte). At 4 bytes per invocation
that is 16384 / 4 = 4096.
No memory access, no state, no traps. Latency does not depend on operand
values. rd may alias rs1/rs2, and rd = x0 discards the result.

