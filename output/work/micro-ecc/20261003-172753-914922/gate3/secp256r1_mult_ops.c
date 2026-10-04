
#include <stdint.h>
#include <string.h>
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

int main(void) {
    uint32_t in[16], out[16];
    for (int i = 0; i < 16; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    secp256r1_mult_model(in, out);
    return (int)(out[0] & 1);
}
