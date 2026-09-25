
#include <stdint.h>
#include <string.h>
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
int main(void) {
    uint32_t in[16], out[16];
    for (int i = 0; i < 16; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    secp256r1_mult_model(in, out);
    return (int)(out[0] & 1);
}
