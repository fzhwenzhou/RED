# Spec review

Reviewers: implementability, callability
Findings: 0 blocking, 2 major, 2 minor

## [MAJOR] spec-wide — implementability
**Finding.** The declared mac_ops=0 implies unrolled combinational logic for all 6 CRC variants, which would require ~4,500 XOR gates and consume an unacceptably large fraction of the minimal core's area.
**Evidence.** A reduction implemented as a bit-serial loop is not a zero-latency operation in hardware unless fully unrolled. Synthesizing six different 32-bit combinational CRC trees duplicates significant logic. The prompt requires flagging any area that is a large fraction of a small core.
**Suggested fix.** Change mac_ops to 32 and implement the 6 variants as a sequential state machine sharing a single 64-bit shift register. The true arithmetic cost of 32 cycles is already bounded by the gate's penalty and retains a >4x speedup.

## [MAJOR] spec-wide — callability
**Finding.** The 32-bit and smaller CRC instructions unnecessarily use the memory-operand ABI, forcing the caller to marshal the CRC state into memory every iteration.
**Evidence.** As the prompt asks: 'Would using it force the caller to keep a value in memory that it currently keeps in registers across a loop?' Yes. The crc32, crc16, crcdnp, crc8, and crcccitt operations take exactly two 32-bit inputs (crc and data) and return one 32-bit output. By using `words=2` (memory block) instead of standard register operands, they force the caller to write its register-held CRC state to memory and load it back on every 4-byte chunk, wasting 52 cycles of marshalling and ~36 cycles of memory bus latency per invocation.
**Suggested fix.** Change the five 32-bit and smaller CRC instructions to be standard PCPI register instructions (`words: 0`, inputs in `rs1` and `rs2`, returning result in `rd`). CRC64 can remain a memory-operand instruction as it requires 3 input registers (crc_l, crc_h, data).

## [MINOR] spec-wide — implementability
**Finding.** The latency of the instructions is heavily dominated by moving operands and coprocessor issue overhead rather than the arithmetic.
**Evidence.** Transferring 2-3 words in and out plus the fixed 28-cycle issue overhead costs 36-40 cycles per invocation. If the arithmetic is unrolled (mac_ops=0, 1 cycle), data movement accounts for 97% of the instruction latency.
**Suggested fix.** Accept the overhead since the application still gains a massive speedup, or consider expanding the operand block to process 16 bytes per invocation in future designs to amortize the fixed 28-cycle issue cost.

## [MINOR] spec-wide — implementability
**Finding.** The unverified invocations declaration (833) is correct and corresponds to a typical data volume, despite the performance gate's implied ratio.
**Evidence.** The performance gate flagged ~397 implied invocations by naively dividing host instructions (~90k) by C model instructions (227). The bit-serial C model is much more instruction-dense than the host table lookups, making this ratio invalid. 833 invocations processing 4 bytes each equals 3332 bytes per call, a standard volume.
**Suggested fix.** No action needed; the mathematical basis for 833 invocations is sound and the predicted speedups are valid.
