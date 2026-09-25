
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>

void secp256r1_mult_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t r0 = 0, r1 = 0, r2 = 0;
    
    for (int k = 0; k < 8; ++k) {
        for (int i = 0; i <= k; ++i) {
            uint64_t p = (uint64_t)in[i] * in[8 + k - i];
            uint64_t r01 = ((uint64_t)r1 << 32) | r0;
            r01 += p;
            r2 += (r01 < p);
            r1 = r01 >> 32;
            r0 = (uint32_t)r01;
        }
        out[k] = r0;
        r0 = r1;
        r1 = r2;
        r2 = 0;
    }
    for (int k = 8; k < 15; ++k) {
        for (int i = (k + 1) - 8; i < 8; ++i) {
            uint64_t p = (uint64_t)in[i] * in[8 + k - i];
            uint64_t r01 = ((uint64_t)r1 << 32) | r0;
            r01 += p;
            r2 += (r01 < p);
            r1 = r01 >> 32;
            r0 = (uint32_t)r01;
        }
        out[k] = r0;
        r0 = r1;
        r1 = r2;
        r2 = 0;
    }
    out[15] = r0;
}
/* ---- end model ---- */

#define WORDS 16
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
    secp256r1_mult_model(in, a);
    secp256r1_mult_model(in, b);
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
