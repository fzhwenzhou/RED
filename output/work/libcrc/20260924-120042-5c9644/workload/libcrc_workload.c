#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include "checksum.h"

#define BUFFER_SIZE 65536
#define NUM_ITERATIONS 200

unsigned char buffer[BUFFER_SIZE];

int main(void) {
    uint32_t state = 1;
    for (size_t i = 0; i < BUFFER_SIZE; i++) {
        state = state * 1103515245 + 12345;
        buffer[i] = (unsigned char)(state >> 16);
    }

    uint64_t sum = 0;

    for (int i = 0; i < NUM_ITERATIONS; i++) {
        buffer[i % BUFFER_SIZE] ^= (unsigned char)i;
        
        sum += crc_8(buffer, BUFFER_SIZE);
        sum += crc_16(buffer, BUFFER_SIZE);
        sum += crc_32(buffer, BUFFER_SIZE);
        sum += crc_64_we(buffer, BUFFER_SIZE);
        sum += crc_ccitt_1d0f(buffer, BUFFER_SIZE);
    }

    printf("%llu\n", (unsigned long long)sum);
    return 0;
}
