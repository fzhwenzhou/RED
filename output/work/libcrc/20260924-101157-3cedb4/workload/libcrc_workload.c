#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "checksum.h"

#define BUF_SIZE 10000
#define ITERATIONS 500

int main(void) {
    unsigned char *buf = malloc(BUF_SIZE);
    if (!buf) return 1;
    
    // Initialize buffer deterministically
    for (int i = 0; i < BUF_SIZE; i++) {
        buf[i] = (unsigned char)((i * 137 + 11) % 256);
    }
    
    uint32_t total_crc32 = 0;
    uint64_t total_crc64 = 0;
    uint16_t total_crc16 = 0;
    uint16_t total_ccitt = 0;
    uint16_t total_dnp = 0;
    uint8_t total_crc8 = 0;
    
    for (int i = 0; i < ITERATIONS; i++) {
        // Slightly modify the buffer so the optimizer doesn't completely remove the loop
        buf[i % BUF_SIZE] ^= (unsigned char)(i % 256);
        
        total_crc32 ^= crc_32(buf, BUF_SIZE);
        total_crc64 ^= crc_64_ecma(buf, BUF_SIZE);
        total_crc16 ^= crc_16(buf, BUF_SIZE);
        total_ccitt ^= crc_ccitt_1d0f(buf, BUF_SIZE);
        total_dnp   ^= crc_dnp(buf, BUF_SIZE);
        total_crc8  ^= crc_8(buf, BUF_SIZE);
    }
    
    // Print the accumulated results to prevent the compiler from optimizing out
    printf("CRC32: %08x\n", total_crc32);
    printf("CRC64: %016llx\n", (unsigned long long)total_crc64);
    printf("CRC16: %04x\n", total_crc16);
    printf("CCITT: %04x\n", total_ccitt);
    printf("DNP:   %04x\n", total_dnp);
    printf("CRC8:  %02x\n", total_crc8);
    
    free(buf);
    return 0;
}
