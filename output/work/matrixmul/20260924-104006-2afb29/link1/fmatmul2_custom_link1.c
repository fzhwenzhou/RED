
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>

void fmatmul2_custom_model(const uint32_t in[8], uint32_t out[8]) {
    float A[4], B[4], C[4];
    memcpy(A, &in[0], 16);
    memcpy(B, &in[4], 16);
    
    C[0] = 0.0f;
    C[0] = C[0] + A[0]*B[0];
    C[0] = C[0] + A[1]*B[2];
    
    C[1] = 0.0f;
    C[1] = C[1] + A[0]*B[1];
    C[1] = C[1] + A[1]*B[3];
    
    C[2] = 0.0f;
    C[2] = C[2] + A[2]*B[0];
    C[2] = C[2] + A[3]*B[2];
    
    C[3] = 0.0f;
    C[3] = C[3] + A[2]*B[1];
    C[3] = C[3] + A[3]*B[3];
    
    memcpy(&out[0], C, 16);
    out[4] = 0;
    out[5] = 0;
    out[6] = 0;
    out[7] = 0;
}
/* ---- end model ---- */

#define WORDS 8
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
    fmatmul2_custom_model(in, a);
    fmatmul2_custom_model(in, b);
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
