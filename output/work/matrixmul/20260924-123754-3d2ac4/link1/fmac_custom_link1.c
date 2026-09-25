
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>

static uint32_t ftz_u32(uint32_t bits) {
    uint32_t exp = (bits >> 23) & 0xFF;
    uint32_t exp_is_zero = (exp - 1) >> 31;
    uint32_t mask = 0xFFFFFFFF ^ (exp_is_zero * 0x7FFFFFFF);
    return bits & mask;
}

static float ftz_f32(float x) {
    uint32_t bits;
    memcpy(&bits, &x, 4);
    bits = ftz_u32(bits);
    memcpy(&x, &bits, 4);
    return x;
}

static uint32_t canon_nan(float x) {
    uint32_t bits;
    memcpy(&bits, &x, 4);
    uint32_t exp = (bits >> 23) & 0xFF;
    uint32_t frac = bits & 0x7FFFFF;
    uint32_t is_nan = (exp == 0xFF) & (frac != 0);
    uint32_t m = (uint32_t)0 - is_nan;
    return (0x7FC00000 & m) | (bits & ~m);
}

void fmac_custom_model(const uint32_t in[3], uint32_t out[3]) {
    float a, b, c;
    uint32_t tmp;

    tmp = ftz_u32(in[0]); memcpy(&a, &tmp, 4);
    tmp = ftz_u32(in[1]); memcpy(&b, &tmp, 4);
    tmp = ftz_u32(in[2]); memcpy(&c, &tmp, 4);

    float p = ftz_f32(a * b);
    uint32_t p_bits = canon_nan(p);
    memcpy(&p, &p_bits, 4);

    float s = ftz_f32(c + p);
    uint32_t s_bits = canon_nan(s);

    out[0] = ftz_u32(s_bits);
    out[1] = 0;
    out[2] = 0;
}
/* ---- end model ---- */

#define WORDS 3
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
    fmac_custom_model(in, a);
    fmac_custom_model(in, b);
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
