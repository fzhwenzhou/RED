
#include <stdint.h>
#include <string.h>
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

int main(void) {
    uint32_t in[17], out[17];
    for (int i = 0; i < 17; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    crc32_step64b_model(in, out);
    return (int)(out[0] & 1);
}
