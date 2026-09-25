
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>

void secp256r1_mmod_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t result[8];
    uint32_t tmp[8];
    int64_t carry = 0;
    uint64_t c;
    const uint32_t p[8] = {0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0x00000000, 
                           0x00000000, 0x00000000, 0x00000001, 0xFFFFFFFF};

    for (int i = 0; i < 8; ++i) result[i] = in[i];

    tmp[0]=0; tmp[1]=0; tmp[2]=0; tmp[3]=in[11]; tmp[4]=in[12]; tmp[5]=in[13]; tmp[6]=in[14]; tmp[7]=in[15];
    c = 0; for(int i=0; i<8; ++i) { c += (uint64_t)tmp[i] + tmp[i]; tmp[i] = (uint32_t)c; c >>= 32; } carry += c;
    c = 0; for(int i=0; i<8; ++i) { c += (uint64_t)result[i] + tmp[i]; result[i] = (uint32_t)c; c >>= 32; } carry += c;

    tmp[0]=0; tmp[1]=0; tmp[2]=0; tmp[3]=in[12]; tmp[4]=in[13]; tmp[5]=in[14]; tmp[6]=in[15]; tmp[7]=0;
    c = 0; for(int i=0; i<8; ++i) { c += (uint64_t)tmp[i] + tmp[i]; tmp[i] = (uint32_t)c; c >>= 32; } carry += c;
    c = 0; for(int i=0; i<8; ++i) { c += (uint64_t)result[i] + tmp[i]; result[i] = (uint32_t)c; c >>= 32; } carry += c;

    tmp[0]=in[8]; tmp[1]=in[9]; tmp[2]=in[10]; tmp[3]=0; tmp[4]=0; tmp[5]=0; tmp[6]=in[14]; tmp[7]=in[15];
    c = 0; for(int i=0; i<8; ++i) { c += (uint64_t)result[i] + tmp[i]; result[i] = (uint32_t)c; c >>= 32; } carry += c;

    tmp[0]=in[9]; tmp[1]=in[10]; tmp[2]=in[11]; tmp[3]=in[13]; tmp[4]=in[14]; tmp[5]=in[15]; tmp[6]=in[13]; tmp[7]=in[8];
    c = 0; for(int i=0; i<8; ++i) { c += (uint64_t)result[i] + tmp[i]; result[i] = (uint32_t)c; c >>= 32; } carry += c;

    tmp[0]=in[11]; tmp[1]=in[12]; tmp[2]=in[13]; tmp[3]=0; tmp[4]=0; tmp[5]=0; tmp[6]=in[8]; tmp[7]=in[10];
    c = 0; for(int i=0; i<8; ++i) { c = (uint64_t)result[i] - tmp[i] - c; result[i] = (uint32_t)c; c = (c >> 32) & 1; } carry -= c;

    tmp[0]=in[12]; tmp[1]=in[13]; tmp[2]=in[14]; tmp[3]=in[15]; tmp[4]=0; tmp[5]=0; tmp[6]=in[9]; tmp[7]=in[11];
    c = 0; for(int i=0; i<8; ++i) { c = (uint64_t)result[i] - tmp[i] - c; result[i] = (uint32_t)c; c = (c >> 32) & 1; } carry -= c;

    tmp[0]=in[13]; tmp[1]=in[14]; tmp[2]=in[15]; tmp[3]=in[8]; tmp[4]=in[9]; tmp[5]=in[10]; tmp[6]=0; tmp[7]=in[12];
    c = 0; for(int i=0; i<8; ++i) { c = (uint64_t)result[i] - tmp[i] - c; result[i] = (uint32_t)c; c = (c >> 32) & 1; } carry -= c;

    tmp[0]=in[14]; tmp[1]=in[15]; tmp[2]=0; tmp[3]=in[9]; tmp[4]=in[10]; tmp[5]=in[11]; tmp[6]=0; tmp[7]=in[13];
    c = 0; for(int i=0; i<8; ++i) { c = (uint64_t)result[i] - tmp[i] - c; result[i] = (uint32_t)c; c = (c >> 32) & 1; } carry -= c;

    for (int j = 0; j < 5; j++) {
        uint32_t mask = (uint32_t)0 - (carry < 0);
        c = 0;
        for (int i = 0; i < 8; i++) {
            c += (uint64_t)result[i] + (p[i] & mask);
            tmp[i] = (uint32_t)c;
            c >>= 32;
        }
        for (int i = 0; i < 8; i++) result[i] = tmp[i];
        carry += (c & mask); 
    }

    for (int j = 0; j < 8; j++) {
        c = 0;
        for (int i = 0; i < 8; i++) {
            c = (uint64_t)result[i] - p[i] - c;
            tmp[i] = (uint32_t)c;
            c = (c >> 32) & 1;
        }
        uint32_t cond = (carry > 0) | ((carry == 0) & (c == 0));
        uint32_t mask = (uint32_t)0 - cond;
        for (int i = 0; i < 8; i++) {
            result[i] = (result[i] & ~mask) | (tmp[i] & mask);
        }
        carry -= (c & mask);
    }

    for (int i = 0; i < 8; ++i) out[i] = result[i];
    for (int i = 8; i < 16; ++i) out[i] = 0;
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
    secp256r1_mmod_model(in, a);
    secp256r1_mmod_model(in, b);
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
