# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 7 blocking, 1 major, 1 minor

## [BLOCKING] `secp256r1.mmod` — implementability
**Finding.** The instruction requires an immense amount of state and a massive FSM that severely violates the area constraints of the small PicoRV32 core.
**Evidence.** The C model executes 6,045 operations (charged as 1,209 sequential logic steps) mimicking constant-time software via 24 iterations of 8-word multi-precision addition and subtraction. Implementing this requires at least a 1,200-state FSM and 48 words (1,536 bits) of registers for the `in`, `out`, `tmp`, and `result` arrays, which vastly exceeds the entire native core's register file (1,024 bits).
**Suggested fix.** Do not hardcode this massive curve-specific algorithm in silicon. Instead, add smaller, general-purpose multi-precision add/sub instructions (e.g., accelerating uECC_vli_add) that the software can compose.

## [BLOCKING] `secp256r1.mmod` — implementability
**Finding.** The instruction causes a stack buffer overflow by writing 16 words to the caller's 8-word result buffer.
**Evidence.** The `words` property is set to 16, meaning the bus-mastering coprocessor will unconditionally write 16 words (64 bytes) to the output address in `rs2`. However, the software function `vli_mmod_fast_secp256r1` provides an 8-word (32-byte) array for its `result`. Writing 16 words (the 8-word reduction plus 8 words of zero-padding) will overwrite 32 bytes of adjacent memory in the caller.
**Suggested fix.** Decouple the input and output block sizes in the ABI so the instruction can read 16 words but only write back 8 words, or rewrite the C model to operate within an 8-word footprint.

## [BLOCKING] `secp256r1.mult` — callability
**Finding.** Replacing `loop1` discards the intermediate carries (`r0`, `r1`, `r2`) required by `loop2`, causing the unmodified software `loop2` to corrupt the upper 256 bits.
**Evidence.** The software's `uECC_vli_mult` carries `r0`, `r1`, and `r2` in local variables across the boundary between `loop1` and `loop2`. The instruction replaces `loop1` and computes the full 512-bit product, writing it to memory, but does not return these carries. The caller's unmodified `loop2` will subsequently execute with uninitialized or zeroed carries, recalculating and overwriting `result[8..15]` with completely wrong values.
**Suggested fix.** The instruction cannot safely replace just `loop1` while computing the full product. Change `replaces` to target a region that covers the entire `uECC_vli_mult` function if one is mined, or design the instruction to compute only the lower half and output the exact carry state `loop2` expects.

## [BLOCKING] `secp256r1.mmod` — callability
**Finding.** The C model implements a massively unoptimized arbitrary-precision reduction loop that will synthesize to a state machine far too large for this minimal core.
**Evidence.** The C model uses deeply nested loops (8- and 16-trip loops surrounding 8-word additions and conditional masks), executing over 6,000 instructions per call and charging 1,209 sequential logic steps. Synthesizing this iterative, software-like loop into a hardware datapath violates the constraint to avoid excessive combinatorial logic and would dwarf the 1024-flop PicoRV32 core just to do the work of 1,204 software instructions.
**Suggested fix.** Rewrite the C model to match the fast secp256r1 reduction algorithm actually used by the software `vli_mmod_fast_secp256r1`, which requires only a fixed, small number of 256-bit additions and subtractions of specific slices of the 512-bit product, vastly reducing the synthesized logic area.

## [BLOCKING] `secp256r1.mult` — benefit
**Finding.** The instruction computes the full 512-bit product but only replaces loop1, leaving loop2 to clobber the upper half of the result with garbage.
**Evidence.** The C model calculates the entire 16-word product (out[0..15]). However, it specifies `replaces: "uECC_vli_mult::loop1"`, which corresponds to only the first half of the software multiply. The second half, `uECC_vli_mult::loop2` (47.3% of cycles), will still execute in software. Because loop2 relies on the carry scalars (r0, r1, r2) flowing from loop1, replacing only loop1 leaves these scalars uninitialized. This causes loop2 to compute incorrect values and overwrite the correct upper half of the result that the instruction just wrote.
**Suggested fix.** Change `replaces` to the entire multiplication function (e.g., `uECC_vli_mult`) so the custom instruction fuses and replaces both loop1 and loop2, preventing memory clobbering and fully capturing the 94.6% of cycles spent in the multiply.

## [BLOCKING] `secp256r1.mult` — legality
**Finding.** The instruction replaces only loop1 but fails to return the intermediate scalar carries (r0, r1, r2) needed by loop2, which will subsequently corrupt the upper half of the result.
**Evidence.** The instruction targets `uECC_vli_mult::loop1`, but its C model computes the full 512-bit product and discards the intermediate `r0, r1, r2` state. Furthermore, the RED ABI only returns memory arrays (via rs2) and a single register (rd=0), providing no mechanism to return the three scalar live-outs required by the unreplaced `loop2`. Software will run `loop2` with stale accumulators and overwrite the hardware's correct upper-half product (`out[8..15]`) with garbage.
**Suggested fix.** Target a region that encompasses the entire multiplication (both loop1 and loop2), as the ABI cannot return multiple scalar live-outs to resume the split loops.

## [BLOCKING] `secp256r1.mmod` — legality
**Finding.** The instruction's words=16 specification forces the coprocessor to write 16 words to the output buffer, causing a buffer overflow in the caller.
**Evidence.** The fixed ABI specifies `rs2 = address of the output block (words x uint32)`. The replaced region `vli_mmod_fast_secp256r1::region` outputs to `result`, which is a 256-bit (8-word) array. The hardware will blindly write 16 words to `rs2`, overflowing this buffer and corrupting adjacent stack memory. The C model reflects this by explicitly zero-padding `out[8..15]`.
**Suggested fix.** To comply with the symmetric-size ABI without overflowing, output the 8-word result in-place back to the 16-word input 'product' buffer (by passing its address to rs2), or reject the instruction.

## [MAJOR] `secp256r1.mult` — implementability
**Finding.** The multiplier instruction requires an operand buffer equal to the entire core's register file and duplicates existing ALU hardware.
**Evidence.** The instruction uses `words: 16`, which necessitates 32 words (1,024 flops) of state buffering for the input and output blocks—matching the total area of the PicoRV32's 32-register file. Additionally, calculating 64 MAC operations requires a dedicated 32x32 multiplier in the coprocessor, duplicating the core's existing pcpi_mul.
**Suggested fix.** Decompose the 256x256 multiplication into smaller operations (e.g., an 8-word by 1-word multiply-add) to cut the state buffer size dramatically and reduce combinatorial logic.

## [MINOR] `secp256r1.mmod` — legality
**Finding.** The marshal_words declaration is inconsistent with the words parameter.
**Evidence.** The instruction declares `marshal_words: 8`, but `words: 16` dictates that the ABI will move 32 words in total (16 in, 16 out). The performance gate underpriced the memory traffic, though correcting it from 206 to ~413 cycles still leaves a >5x whole-application speedup.
**Suggested fix.** Update marshal_words to 16 to match the words parameter.
