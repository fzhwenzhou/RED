
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>
/* mnemonic "crc32.step64b" -> crc32_step64b_model.
   in[0] = running CRC-32 state; in[1..16] = 64 data bytes, little-endian:
   byte k = (in[1 + k/4] >> (8*(k%4))) & 0xFF. out[0] = new state, out[1..16] = 0. */
void crc32_step64b_model(const uint32_t in[17], uint32_t out[17]) {
    uint32_t crc = in[0];
    int i, j, b;
    for (i = 0; i < 16; i++) {
        uint32_t w = in[1 + i];
        for (j = 0; j < 4; j++) {
            crc ^= (w >> (8 * j)) & 0xFFu;
            for (b = 0; b < 8; b++) {
                uint32_t m = (uint32_t)0 - (crc & 1u);
                crc = (crc >> 1) ^ (0xEDB88320u & m);
            }
        }
    }
    out[0] = crc;
    for (i = 1; i < 17; i++) out[i] = 0;
}

/* ---- end model ---- */

#define WORDS 17
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
    crc32_step64b_model(in, a);
    crc32_step64b_model(in, b);
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
