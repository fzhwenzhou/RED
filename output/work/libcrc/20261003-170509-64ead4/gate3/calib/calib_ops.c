
#include <stdint.h>
#include <string.h>

void calib_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t a[8], b[8];
    for (int i = 0; i < 8; i++) { a[i] = in[i]; b[i] = in[i + 8]; }
    for (int i = 0; i < 16; i++) out[i] = 0;
    for (int i = 0; i < 8; i++) {
        uint32_t carry = 0;
        for (int j = 0; j < 8; j++) {
            uint64_t t = (uint64_t)a[i] * b[j] + out[i + j] + carry;
            out[i + j] = (uint32_t)t;
            carry = (uint32_t)(t >> 32);
        }
        out[i + 8] = carry;
    }
}

int main(void) {
    uint32_t in[16], out[16];
    for (int i = 0; i < 16; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    calib_model(in, out);
    return (int)(out[0] & 1);
}
