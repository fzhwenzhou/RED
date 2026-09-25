
#include <stdint.h>
#include <string.h>
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
int main(void) {
    uint32_t in[2], out[2];
    for (int i = 0; i < 2; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    crcccitt_step32b_model(in, out);
    return (int)(out[0] & 1);
}
