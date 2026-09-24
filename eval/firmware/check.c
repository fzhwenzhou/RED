/*  check.c — is the patched uECC_vli_mult (which uses mult256) still correct?
 *  Compares it against a plain-C schoolbook multiply that uses no extension. */
#include <stdint.h>
#include "uECC.h"
#include "uECC_vli.h"

#define MMIO_PUTC (*(volatile uint32_t *)0x10000000)
static void putc_(char c) { MMIO_PUTC = (uint32_t)(uint8_t)c; }
static void puts_(const char *s) { while (*s) putc_(*s++); }
static void puthex32_(uint32_t v) {
	static const char h[] = "0123456789abcdef";
	for (int i = 7; i >= 0; i--) putc_(h[(v >> (4 * i)) & 15]);
}
void *memcpy(void *d, const void *s, unsigned long n) {
	uint8_t *dd=d; const uint8_t *ss=s; while (n--) *dd++=*ss++; return d; }
void *memset(void *d, int c, unsigned long n) {
	uint8_t *dd=d; while (n--) *dd++=(uint8_t)c; return d; }

static void ref_mult(uint32_t *r, const uint32_t *a, const uint32_t *b) {
	for (int i = 0; i < 16; i++) r[i] = 0;
	for (int i = 0; i < 8; i++) {
		uint32_t carry = 0;
		for (int j = 0; j < 8; j++) {
			uint64_t t = (uint64_t)a[i] * b[j] + r[i + j] + carry;
			r[i + j] = (uint32_t)t;
			carry = (uint32_t)(t >> 32);
		}
		r[i + 8] = carry;
	}
}

int main(void) {
	uint32_t a[8], b[8], got[16], ref[16];
	int bad = 0;
	uint32_t s = 0x12345678u;
	for (int t = 0; t < 256; t++) {
		for (int i = 0; i < 8; i++) {
			s ^= s << 13; s ^= s >> 17; s ^= s << 5; a[i] = s;
			s ^= s << 13; s ^= s >> 17; s ^= s << 5; b[i] = s;
		}
		if (t == 0) for (int i = 0; i < 8; i++) { a[i] = 0xffffffffu; b[i] = 0xffffffffu; }
		ref_mult(ref, a, b);
		uECC_vli_mult(got, a, b, 8);
		for (int i = 0; i < 16; i++) if (got[i] != ref[i]) {
			if (!bad) {
				puts_("MISMATCH trial "); putc_((char)('0'+t));
				puts_(" word "); putc_((char)('0'+i));
				puts_("\n  uECC_vli_mult "); puthex32_(got[i]);
				puts_("\n  reference     "); puthex32_(ref[i]); putc_('\n');
			}
			bad++;
		}
	}
	puts_(bad ? "RESULT: FAIL\n" : "uECC_vli_mult matches reference\nRESULT: PASS\n");
	return bad ? 1 : 0;
}
