
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>
/* mnemonic secp256r1.modsub -> secp256r1_modsub_model; in[0..7]=L, in[8..15]=R, out[0..7]=(L-R) mod p256, out[8..15]=0 */
static const uint32_t MS_P256[8] = {0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFFu, 0x00000000u,
                                    0x00000000u, 0x00000000u, 0x00000001u, 0xFFFFFFFFu};
void secp256r1_modsub_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t d[8];
    uint64_t b = 0, c = 0;
    uint32_t m;
    int i;
    for (i = 0; i < 8; i++) {
        uint64_t s = (uint64_t)in[i] - (uint64_t)in[8 + i] - b;
        d[i] = (uint32_t)s;
        b = (s >> 32) & 1u;
    }
    m = (uint32_t)0 - (uint32_t)b;
    for (i = 0; i < 8; i++) {
        uint64_t s = (uint64_t)d[i] + (uint64_t)(MS_P256[i] & m) + c;
        out[i] = (uint32_t)s;
        c = s >> 32;
    }
    for (i = 8; i < 16; i++) out[i] = 0;
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
    secp256r1_modsub_model(in, a);
    secp256r1_modsub_model(in, b);
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
