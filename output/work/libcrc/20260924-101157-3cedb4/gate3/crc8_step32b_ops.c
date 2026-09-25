
#include <stdint.h>
#include <string.h>
void crc8_step32b_model(const uint32_t in[2], uint32_t out[2]) {
    uint8_t crc = (uint8_t)in[0];
    uint32_t data = in[1];
    for(int i=0; i<4; i++) {
        uint8_t val = (data >> (i*8)) & 0xFF;
        uint8_t c = crc ^ val;
        for(int j=0; j<8; j++) {
            uint8_t mask = (uint8_t)0 - (c >> 7);
            c = (c << 1) ^ (0x31 & mask);
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
    crc8_step32b_model(in, out);
    return (int)(out[0] & 1);
}
