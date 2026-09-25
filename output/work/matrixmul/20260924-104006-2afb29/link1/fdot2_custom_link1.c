
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>

static inline uint32_t nan_canon(uint32_t u) {
    uint32_t exp = (u >> 23) & 0xFFu;
    uint32_t frac = u & 0x7FFFFFu;
    uint32_t sign = u & 0x80000000u;
    
    uint32_t exp_nz = (exp | (0u - exp)) >> 31;
    uint32_t exp_zero_mask = (exp_nz ^ 1u) * 0xFFFFFFFFu;
    
    uint32_t frac_nz = (frac | (0u - frac)) >> 31;
    uint32_t frac_nz_mask = frac_nz * 0xFFFFFFFFu;
    
    uint32_t subnormal_mask = exp_zero_mask & frac_nz_mask;
    
    uint32_t exp_not_ff = exp ^ 0xFFu;
    uint32_t exp_not_ff_nz = (exp_not_ff | (0u - exp_not_ff)) >> 31;
    uint32_t exp_ff_mask = (exp_not_ff_nz ^ 1u) * 0xFFFFFFFFu;
    
    uint32_t nan_mask = exp_ff_mask & frac_nz_mask;
    
    uint32_t flushed = (u & ~subnormal_mask) | (sign & subnormal_mask);
    uint32_t res = (flushed & ~nan_mask) | (0x7FC00000u & nan_mask);
    
    return res;
}

static inline float ftz(float f) {
    uint32_t u;
    memcpy(&u, &f, 4);
    u = nan_canon(u);
    float res;
    memcpy(&res, &u, 4);
    return res;
}

void fdot2_custom_model(const uint32_t in[5], uint32_t out[5]) {
    float c, a0, a1, b0, b1;
    
    memcpy(&c,  &in[0], 4);
    memcpy(&a0, &in[1], 4);
    memcpy(&a1, &in[2], 4);
    memcpy(&b0, &in[3], 4);
    memcpy(&b1, &in[4], 4);
    
    c = ftz(c);
    a0 = ftz(a0);
    a1 = ftz(a1);
    b0 = ftz(b0);
    b1 = ftz(b1);
    
    float p0 = ftz(a0 * b0);
    float sum1 = ftz(c + p0);
    
    float p1 = ftz(a1 * b1);
    float sum2 = ftz(sum1 + p1);
    
    uint32_t res_u;
    memcpy(&res_u, &sum2, 4);
    res_u = nan_canon(res_u);
    
    out[0] = res_u;
    out[1] = 0;
    out[2] = 0;
    out[3] = 0;
    out[4] = 0;
}
/* ---- end model ---- */

#define WORDS 5
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
    fdot2_custom_model(in, a);
    fdot2_custom_model(in, b);
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
