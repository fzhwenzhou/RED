
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
void crc64_ecma_step32b_model(const uint32_t in[3], uint32_t out[3]) {
    uint64_t crc = ((uint64_t)in[1] << 32) | in[0];
    uint32_t data = in[2];
    for(int i=0; i<4; i++) {
        uint8_t val = (data >> (i*8)) & 0xFF;
        uint64_t c = crc ^ ((uint64_t)val << 56);
        for(int j=0; j<8; j++) {
            uint64_t mask = (uint64_t)0 - (c >> 63);
            c = (c << 1) ^ (0x42F0E1EBA9EA3693ull & mask);
        }
        crc = c;
    }
    out[0] = (uint32_t)crc;
    out[1] = (uint32_t)(crc >> 32);
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
    crc64_ecma_step32b_model(in, a);
    crc64_ecma_step32b_model(in, b);
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
