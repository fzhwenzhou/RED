
#include <stdint.h>
#include <string.h>
void secp256r1_mult_model(const uint32_t in[16], uint32_t out[16]) {
    for (int i = 0; i < 16; i++) out[i] = 0;
    for (int i = 0; i < 8; i++) {
        uint64_t carry = 0;
        for (int j = 0; j < 8; j++) {
            uint64_t prod = (uint64_t)in[i] * in[8 + j];
            uint64_t sum = (uint64_t)out[i + j] + prod + carry;
            out[i + j] = (uint32_t)sum;
            carry = sum >> 32;
        }
        out[i + 8] = (uint32_t)carry;
    }
}
int main(void) {
    uint32_t in[16], out[16];
    for (int i = 0; i < 16; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    secp256r1_mult_model(in, out);
    return (int)(out[0] & 1);
}
