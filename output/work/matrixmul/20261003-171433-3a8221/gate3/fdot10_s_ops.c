
#include <stdint.h>
#include <string.h>
#include <stdint.h>
#include <string.h>

/* mnemonic "fdot10.s" -> fdot10_s_model.
 * in[2k]=A[i][k], in[2k+1]=B[k][j] for k=0..9 ; in[20]=starting accumulator c.
 * out[0] = canon(((c + a0*b0) + a1*b1) + ... + a9*b9), every op a separately
 * rounded binary32 RNE operation; out[1..20] = 0. */
static float f32_from_bits(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
static uint32_t f32_to_bits(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }

void fdot10_s_model(const uint32_t in[21], uint32_t out[21]) {
    float acc = f32_from_bits(in[20]);
    int k;
    for (k = 0; k < 10; k++) {
        float a = f32_from_bits(in[2 * k]);
        float b = f32_from_bits(in[2 * k + 1]);
        /* separate statements: product and sum rounded on their own (no contraction) */
        float p = a * b;
        acc = acc + p;
    }
    {
        uint32_t r = f32_to_bits(acc);
        uint32_t is_nan = (uint32_t)((r & 0x7FFFFFFFu) > 0x7F800000u);
        uint32_t m = (uint32_t)0 - is_nan;
        out[0] = (0x7FC00000u & m) | (r & ~m);
    }
    for (k = 1; k < 21; k++) out[k] = 0;
}

int main(void) {
    uint32_t in[21], out[21];
    for (int i = 0; i < 21; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    fdot10_s_model(in, out);
    return (int)(out[0] & 1);
}
