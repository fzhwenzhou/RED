
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>
/* mnemonic secp256r1.mult -> secp256r1_mult_model; in[0..7]=A, in[8..15]=B, out[0..15]=A*B */
void secp256r1_mult_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t r0 = 0, r1 = 0, r2 = 0;
    int k, i;
    for (k = 0; k < 15; k++) {
        for (i = 0; i < 8; i++) {
            int j = k - i;
            if (j >= 0 && j < 8) {
                uint64_t p = (uint64_t)in[i] * (uint64_t)in[8 + j];
                uint64_t lo = (uint64_t)r0 + (uint64_t)(uint32_t)p;
                uint64_t hi = (uint64_t)r1 + (p >> 32) + (lo >> 32);
                r0 = (uint32_t)lo;
                r1 = (uint32_t)hi;
                r2 = r2 + (uint32_t)(hi >> 32);
            }
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
