# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 4 blocking, 1 major, 0 minor

## [BLOCKING] `secp256r1.mmod` — benefit+implementability+legality
**Finding.** The instruction requires an immense amount of state and a massive FSM that severely violates the area constraints of the small PicoRV32 core.
**Evidence.** The C model implements an unoptimized arbitrary-precision reduction loop executing 6,045 instructions, which the performance gate evaluated as 1209 sequential logic steps. Synthesizing this into a hardware FSM and datapath would require thousands of flops, drastically exceeding the entire PicoRV32 core's budget of 1024 flops.
**Suggested fix.** Drop the instruction, or redesign it as a minimal hardware primitive that reuses the core's ALU instead of embedding an arbitrary-precision reduction loop in hardware.

## [BLOCKING] `secp256r1.mmod` — benefit+implementability
**Finding.** The instruction causes a stack buffer overflow by writing 16 words to the caller's 8-word result buffer.
**Evidence.** The instruction replaces vli_mmod_fast_secp256r1::region, where the caller provides an 8-word result buffer. The spec defines words=16 and semantics that zero-pad the output to 16 words. Writing 16 words to the caller's 8-word pointer will overwrite adjacent stack memory, despite the designer's note to 'alias to the product buffer' which unmodified software will not do.
**Suggested fix.** Change the ABI operand block to decouple input and output sizes, or drop the instruction if the fixed ABI cannot safely return an 8-word result from a 16-word input.

## [BLOCKING] `secp256r1.mmod` — callability
**Finding.** The C model implements a massively unoptimized arbitrary-precision reduction loop that will synthesize to a state machine far too large for this minimal core.
**Evidence.** The C model executes 6,045 instructions (evaluated as 1209 sequential logic steps by the performance gate) for a single reduction, heavily relying on deeply nested loops and 64-bit arithmetic. Synthesizing this into a single hardwired state machine severely violates the PicoRV32 area constraints (the entire core register file is 1024 flops).
**Suggested fix.** Remove the secp256r1.mmod instruction entirely. The secp256r1.mult instruction alone provides a 15x speedup over a large portion of the profile, which is highly successful without bloating the core area.

## [BLOCKING] `secp256r1.mmod` — callability+legality
**Finding.** The instruction's words=16 specification forces the coprocessor to write 16 words to the output buffer, causing a stack buffer overflow in the caller.
**Evidence.** The RED ABI uses the single 'words' parameter (16) for both the input and output block sizes. The original application caller allocates exactly 8 words for the reduction result (uint32_t tmp[num_words_secp256r1]). When the instruction executes, the hardware will blindly write 16 words into the 8-word 'result' destination, corrupting adjacent stack memory.
**Suggested fix.** Remove the secp256r1.mmod instruction, as the RED ABI's symmetric operand block sizes cannot safely accommodate a 512-bit to 256-bit reduction without manual application rewrites.

## [MAJOR] `secp256r1.mult` — implementability
**Finding.** The multiplier instruction requires an operand buffer equal to the entire core's register file and duplicates existing ALU hardware.
**Evidence.** With words=16, the instruction requires 512 bits for input and 512 bits for output, heavily inflating the footprint of a minimal core. Additionally, it duplicates the hardware cost of 32x32 multiplies that the PicoRV32 already has available.
**Suggested fix.** Redesign the instruction to operate on smaller operand sizes (e.g., 4x4 words) to reduce flip-flop usage and reuse the existing CPU datapath.
