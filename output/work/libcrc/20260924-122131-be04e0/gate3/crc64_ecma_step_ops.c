
#include <stdint.h>
#include <string.h>
#include <stdint.h>
void crc64_ecma_step_model(const uint32_t in[3], uint32_t out[3]) {
    uint64_t crc = ((uint64_t)in[1] << 32) | in[0];
    uint32_t data = in[2];
    for (int i = 0; i < 4; i++) {
        uint64_t byte = (data >> (i * 8)) & 0xFF;
        crc = crc ^ (byte << 56);
        for (int b = 0; b < 8; b++) {
            uint64_t mask = (uint64_t)0 - ((crc >> 63) & 1);
            crc = (crc << 1) ^ (0x42F0E1EBA9EA3693ull & mask);
        }
    }
    out[0] = (uint32_t)crc;
    out[1] = (uint32_t)(crc >> 32);
    out[2] = 0;
}
int main(void) {
    uint32_t in[3], out[3];
    for (int i = 0; i < 3; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    crc64_ecma_step_model(in, out);
    return (int)(out[0] & 1);
}
