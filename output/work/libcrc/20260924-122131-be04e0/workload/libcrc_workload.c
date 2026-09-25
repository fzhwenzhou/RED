#include <stdint.h>
#include <stddef.h>
#include "checksum.h"

#define BUFFER_SIZE 32768
#define ITERATIONS  1000

int main(void) {
    uint8_t buffer[BUFFER_SIZE];
    for (size_t i = 0; i < BUFFER_SIZE; i++) {
        buffer[i] = (uint8_t)((i * 1103515245 + 12345) >> 16);
    }

    uint32_t total_crc32 = 0;
    uint64_t total_crc64 = 0;
    uint16_t total_crc16 = 0;

    for (int i = 0; i < ITERATIONS; i++) {
        // slightly shift the buffer start to prevent trivial caching/optimization
        size_t offset = i % 16;
        size_t len = BUFFER_SIZE - offset;
        total_crc32 ^= crc_32(buffer + offset, len);
        total_crc64 ^= crc_64_ecma(buffer + offset, len);
        total_crc16 ^= crc_16(buffer + offset, len);
    }

    if (total_crc32 == 0xdeadbeef) { // dummy check to use the result
        return 1;
    }
    
    // Checksum verification can just be returned as part of a 0 check.
    // For deterministic exit 0:
    return (total_crc32 != 0 && total_crc64 != 0 && total_crc16 != 0) ? 0 : 1;
}
