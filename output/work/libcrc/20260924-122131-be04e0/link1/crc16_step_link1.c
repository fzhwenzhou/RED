
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
void crc16_step_model(const uint32_t in[3], uint32_t out[3]) {
    uint32_t crc = in[0] & 0xFFFF;
    for (int w = 0; w < 2; w++) {
        uint32_t data = in[w + 1];
        for (int i = 0; i < 4; i++) {
            uint32_t byte = (data >> (i * 8)) & 0xFF;
            crc = crc ^ byte;
            for (int b = 0; b < 8; b++) {
                uint32_t mask = (uint32_t)0 - (crc & 1);
                crc = (crc >> 1) ^ (0xA001 & mask);
            }
        }
    }
    out[0] = crc;
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
    crc16_step_model(in, a);
    crc16_step_model(in, b);
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
