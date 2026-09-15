/*
 *  bench.c — the end-to-end workload: one full ECDH key exchange over
 *  secp256r1 with micro-ecc, on PicoRV32.
 *
 *  This is the whole application, not a kernel microbenchmark: two key pairs
 *  are generated and both sides derive the shared secret, which is then
 *  checked to agree. The same source builds the baseline and the extended
 *  binary — only micro-ecc underneath differs (see eval/patches/) — so a
 *  cycle difference is the extension's, and a wrong secret is caught here
 *  rather than being reported as a speedup.
 *
 *  The RNG is deterministic on purpose: both binaries must do exactly the
 *  same work for their cycle counts to be comparable.
 */

#include <stdint.h>
#include "uECC.h"

#define MMIO_PUTC   (*(volatile uint32_t *)0x10000000)
#define MMIO_EXIT   (*(volatile uint32_t *)0x10000004)
#define MMIO_CYCLES (*(volatile uint32_t *)0x10000008)

static void putc_(char c) { MMIO_PUTC = (uint32_t)(uint8_t)c; }
static void puts_(const char *s) { while (*s) putc_(*s++); }

static void putu_(uint32_t v) {
	char buf[11]; int n = 0;
	if (!v) { putc_('0'); return; }
	while (v) { buf[n++] = (char)('0' + v % 10); v /= 10; }
	while (n) putc_(buf[--n]);
}

static void puthex_(const uint8_t *p, int len) {
	static const char hex[] = "0123456789abcdef";
	for (int i = 0; i < len; i++) { putc_(hex[p[i] >> 4]); putc_(hex[p[i] & 15]); }
}

/* micro-ecc needs these two; freestanding build has no libc. */
void *memcpy(void *d, const void *s, unsigned long n) {
	uint8_t *dd = d; const uint8_t *ss = s;
	while (n--) *dd++ = *ss++;
	return d;
}
void *memset(void *d, int c, unsigned long n) {
	uint8_t *dd = d;
	while (n--) *dd++ = (uint8_t)c;
	return d;
}

/* Deterministic xorshift32 "RNG" — reproducibility beats entropy here. */
static uint32_t rng_state = 0x12345678u;
static int det_rng(uint8_t *dest, unsigned size) {
	while (size--) {
		rng_state ^= rng_state << 13;
		rng_state ^= rng_state >> 17;
		rng_state ^= rng_state << 5;
		*dest++ = (uint8_t)rng_state;
	}
	return 1;
}

int main(void) {
	const struct uECC_Curve_t *curve = uECC_secp256r1();
	uint8_t priv1[32], pub1[64], priv2[32], pub2[64], sec1[32], sec2[32];
	uint32_t t0, t_key, t_ecdh, total;
	int ok = 1;

	uECC_set_rng(&det_rng);

	puts_("micro-ecc ECDH secp256r1 on PicoRV32\n");

	t0 = MMIO_CYCLES;
	if (!uECC_make_key(pub1, priv1, curve)) { puts_("make_key 1 FAILED\n"); ok = 0; }
	if (!uECC_make_key(pub2, priv2, curve)) { puts_("make_key 2 FAILED\n"); ok = 0; }
	t_key = MMIO_CYCLES - t0;

	t0 = MMIO_CYCLES;
	if (!uECC_shared_secret(pub2, priv1, sec1, curve)) { puts_("ecdh 1 FAILED\n"); ok = 0; }
	if (!uECC_shared_secret(pub1, priv2, sec2, curve)) { puts_("ecdh 2 FAILED\n"); ok = 0; }
	t_ecdh = MMIO_CYCLES - t0;

	for (int i = 0; i < 32; i++) if (sec1[i] != sec2[i]) ok = 0;

	puts_("shared secret : "); puthex_(sec1, 32); putc_('\n');
	puts_("secrets agree : "); puts_(ok ? "yes\n" : "NO\n");
	puts_("cycles make_key(x2)      : "); putu_(t_key);  putc_('\n');
	puts_("cycles shared_secret(x2) : "); putu_(t_ecdh); putc_('\n');
	total = t_key + t_ecdh;
	puts_("cycles TOTAL             : "); putu_(total);  putc_('\n');
	puts_(ok ? "RESULT: PASS\n" : "RESULT: FAIL\n");
	return ok ? 0 : 1;
}
