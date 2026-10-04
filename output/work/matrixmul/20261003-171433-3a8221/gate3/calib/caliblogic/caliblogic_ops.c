
#include <stdint.h>
#include <string.h>

void caliblogic_model(const uint32_t in[8], uint32_t out[8]) {
    uint32_t crc = in[0];
    for (int i = 0; i < 8; i++) {
        uint32_t byte = in[i];
        for (int b = 0; b < 32; b++) {
            uint32_t mask = (uint32_t)0 - ((crc ^ byte) & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
            byte >>= 1;
        }
        out[i] = crc;
    }
}

int main(void) {
    uint32_t in[8], out[8];
    for (int i = 0; i < 8; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    caliblogic_model(in, out);
    return (int)(out[0] & 1);
}
