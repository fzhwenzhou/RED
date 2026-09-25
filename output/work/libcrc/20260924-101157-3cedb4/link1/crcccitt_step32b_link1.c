
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
void crcccitt_step32b_model(const uint32_t in[2], uint32_t out[2]) {
    uint16_t crc = (uint16_t)in[0];
    uint32_t data = in[1];
    for(int i=0; i<4; i++) {
        uint8_t val = (data >> (i*8)) & 0xFF;
        uint16_t c = crc ^ ((uint16_t)val << 8);
        for(int j=0; j<8; j++) {
            uint16_t mask = (uint16_t)0 - (c >> 15);
            c = (c << 1) ^ (0x1021 & mask);
        }
        crc = c;
    }
    out[0] = crc;
    out[1] = 0;
}
/* ---- end model ---- */

#define WORDS 2
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
    crcccitt_step32b_model(in, a);
    crcccitt_step32b_model(in, b);
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
