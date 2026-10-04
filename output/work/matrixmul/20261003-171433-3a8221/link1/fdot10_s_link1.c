
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
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

/* ---- end model ---- */

#define WORDS 21
#define NRAND 100000

static uint32_t xs32(uint32_t *s) {
    uint32_t x = *s;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return *s = x;
}

static int run_vector(const uint32_t *in, long idx) {
    uint32_t a[WORDS], b[WORDS];
    memset(a, 0, sizeof a);
    memset(b, 0, sizeof b);
    fdot10_s_model(in, a);
    fdot10_s_model(in, b);
    if (memcmp(a, b, sizeof a) != 0) {
        printf("FAIL vector %ld: model is not a pure function of its input\n", idx);
        return 1;
    }
    return 0;
}

int main(void) {
    uint32_t in[WORDS];
    const uint32_t corners[] = { 0x00000000u, 0xFFFFFFFFu, 0xAAAAAAAAu, 0x55555555u };
    long n = 0;

    for (unsigned c = 0; c < sizeof corners / sizeof corners[0]; c++, n++) {
        for (int w = 0; w < WORDS; w++) in[w] = corners[c];
        if (run_vector(in, n)) return 1;
    }
    uint32_t seed = 0xC0FFEEu;
    for (long i = 0; i < NRAND; i++, n++) {
        for (int w = 0; w < WORDS; w++) in[w] = xs32(&seed);
        if (run_vector(in, n)) return 1;
    }
    printf("PASS %ld vectors\n", n);
    return 0;
}
