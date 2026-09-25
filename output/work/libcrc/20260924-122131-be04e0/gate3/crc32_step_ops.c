
#include <stdint.h>
#include <string.h>
#include <stdint.h>
void crc32_step_model(const uint32_t in[3], uint32_t out[3]) {
    uint32_t crc = in[0];
    for (int w = 0; w < 2; w++) {
        uint32_t data = in[w + 1];
        for (int i = 0; i < 4; i++) {
            uint32_t byte = (data >> (i * 8)) & 0xFF;
            crc = crc ^ byte;
            for (int b = 0; b < 8; b++) {
                uint32_t mask = (uint32_t)0 - (crc & 1);
                crc = (crc >> 1) ^ (0xEDB88320 & mask);
            }
        }
    }
    out[0] = crc;
    out[1] = 0;
    out[2] = 0;
}
int main(void) {
    uint32_t in[3], out[3];
    for (int i = 0; i < 3; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    crc32_step_model(in, out);
    return (int)(out[0] & 1);
}
