#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "checksum.h"

#define BUF_SIZE (64 * 1024)
#define ITERS    600

static unsigned char buf[BUF_SIZE];

int main(void) {
	uint32_t x = 0x12345678u;
	size_t i;
	for (i = 0; i < BUF_SIZE; i++) {
		x = x * 1664525u + 1013904223u;
		buf[i] = (unsigned char)(x >> 24);
	}

	uint32_t acc = 0;
	for (int it = 0; it < ITERS; it++) {
		/* vary the input each pass so no call can be hoisted */
		buf[it % BUF_SIZE] ^= (unsigned char)acc;
		/* mix of packet sizes: one full buffer plus a few frame-sized chunks */
		acc ^= crc_32(buf, BUF_SIZE);
		acc = (acc << 1) | (acc >> 31);
		acc += crc_32(buf + (it & 1023), 1500);
	}

	printf("crc32 checksum: %08x\n", (unsigned)acc);
	return acc == 0xDEADBEEFu ? 1 : 0;
}
