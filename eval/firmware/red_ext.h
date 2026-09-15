/*
 *  red_ext.h — intrinsics for the RED-designed extension.
 *
 *  One inline asm per instruction, using the encoding from the ISASpec and
 *  RED's operand ABI: rs1 = &input block, rs2 = &output block, rd <- 0.
 *  `.insn r` lets the assembler build a custom-opcode R-type without needing
 *  a patched binutils.
 *
 *      mac96   funct3=0  words=5   in[0..2]=96b acc, in[3]=a, in[4]=b
 *      add256  funct3=1  words=16  in[0..7]=x, in[8..15]=y -> out[0..7]
 *      sub256  funct3=2  words=16  in[0..7]=x, in[8..15]=y -> out[0..7]
 *
 *  The memory operands mean the compiler cannot see the data flow, so every
 *  intrinsic is a full memory clobber.
 */
#ifndef RED_EXT_H_
#define RED_EXT_H_

#include <stdint.h>

#define RED_CUSTOM0 0x0b

#define RED_INSN(funct3, in, out)                                             \
	do {                                                                      \
		register const uint32_t *_i __asm__("a0") = (in);                     \
		register uint32_t       *_o __asm__("a1") = (out);                    \
		uint32_t _z;                                                          \
		__asm__ __volatile__(".insn r %3, %4, 0, %0, %1, %2"                  \
		    : "=r"(_z)                                                        \
		    : "r"(_i), "r"(_o), "i"(RED_CUSTOM0), "i"(funct3)                 \
		    : "memory");                                                      \
		(void)_z;                                                             \
	} while (0)

/* 96-bit accumulator += a*b.  io[0..2] = acc, io[3] = a, io[4] = b */
static inline void red_mac96(const uint32_t in[5], uint32_t out[5])
{ RED_INSN(0, in, out); }

/* out[0..7] = in[0..7] + in[8..15]   (carry-out is NOT produced) */
static inline void red_add256(const uint32_t in[16], uint32_t out[16])
{ RED_INSN(1, in, out); }

/* out[0..7] = in[0..7] - in[8..15]   (borrow-out is NOT produced) */
static inline void red_sub256(const uint32_t in[16], uint32_t out[16])
{ RED_INSN(2, in, out); }

#endif
