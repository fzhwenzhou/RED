/* Intrinsic for eval/spec/micro-ecc.json. */
#ifndef RED_EXT_H_
#define RED_EXT_H_
#include <stdint.h>
#define RED_CUSTOM0 0x0b
#define RED_INSN(funct3, in, out)                                             \
    do {                                                                      \
        register const uint32_t *_i __asm__("a0") = (in);                     \
        register uint32_t *_o __asm__("a1") = (out);                          \
        uint32_t _z;                                                          \
        __asm__ __volatile__(".insn r %3, %4, 0, %0, %1, %2"                  \
            : "=r"(_z)                                                        \
            : "r"(_i), "r"(_o), "i"(RED_CUSTOM0), "i"(funct3)                \
            : "memory");                                                      \
        (void)_z;                                                             \
    } while (0)
/* in[0..7] * in[8..15] -> out[0..15], little-endian 32-bit limbs. */
static inline void red_mult256(const uint32_t in[16], uint32_t out[16])
{ RED_INSN(0, in, out); }
#endif
