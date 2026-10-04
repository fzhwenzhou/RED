
#include <stdint.h>
#include <string.h>
#include <valgrind/memcheck.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>

/* mnemonic "fgemm10.s" -> fgemm10_s_model.
 * in[0..99]    = A, 10x10 row-major binary32: A[i][k] = in[10*i + k]
 * in[100..199] = B, 10x10 row-major binary32: B[k][j] = in[100 + 10*k + j]
 * out[10*i + j] = canon((((+0 + A[i][0]*B[0][j]) + A[i][1]*B[1][j]) + ...) + A[i][9]*B[9][j])
 * every multiply and every add separately rounded binary32 RNE; out[100..199] = 0. */
static float f32_from_bits(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
static uint32_t f32_to_bits(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }

void fgemm10_s_model(const uint32_t in[200], uint32_t out[200]) {
    int i, j, k;
    for (i = 0; i < 10; i++) {
        for (j = 0; j < 10; j++) {
            float acc = f32_from_bits(0x00000000u);
            uint32_t r, is_nan, m;
            for (k = 0; k < 10; k++) {
                float a = f32_from_bits(in[10 * i + k]);
                float b = f32_from_bits(in[100 + 10 * k + j]);
                /* separate statements: product and sum rounded on their own (no contraction) */
                float p = a * b;
                acc = acc + p;
            }
            r = f32_to_bits(acc);
            is_nan = (uint32_t)((r & 0x7FFFFFFFu) > 0x7F800000u);
            m = (uint32_t)0 - is_nan;
            out[10 * i + j] = (0x7FC00000u & m) | (r & ~m);
        }
    }
    for (i = 100; i < 200; i++) out[i] = 0;
}

/* ---- end model ---- */

#define WORDS 200

/* The result is consumed through a volatile sink rather than through the exit
   status, so the process's exit code carries memcheck's verdict and nothing
   else: 0 is clean, 97 is "memcheck found errors", anything else means
   memcheck itself could not run. */
static volatile uint32_t sink;

int main(void) {
    uint32_t in[WORDS], out[WORDS];
    int w;
    for (w = 0; w < WORDS; w++) in[w] = 0x9e3779b9u * (uint32_t)(w + 1);
    memset(out, 0, sizeof out);
    VALGRIND_MAKE_MEM_UNDEFINED(in, sizeof in);
    fgemm10_s_model(in, out);
    VALGRIND_MAKE_MEM_DEFINED(out, sizeof out);
    for (w = 0; w < WORDS; w++) sink = out[w];
    return 0;
}
