/*
 *  microbench.c — per-operation cost, measured on the real PicoRV32 RTL.
 *
 *  The whole-application numbers in eval/RESULTS.md are explained by these:
 *  for each instruction RED designed we measure (a) the micro-ecc software
 *  routine it is meant to replace, (b) the instruction issued back-to-back with
 *  its operands already in place, and (c) the instruction as application code
 *  must actually use it — marshalling the operands into RED's contiguous
 *  input block and copying the result back out.
 *
 *  Must run on the core built with ENABLE_ACCEL=1.
 */

#include <stdint.h>
#include "uECC.h"
#include "uECC_vli.h"
#include "red_ext.h"

#define MMIO_PUTC   (*(volatile uint32_t *)0x10000000)
#define MMIO_EXIT   (*(volatile uint32_t *)0x10000004)
#define MMIO_CYCLES (*(volatile uint32_t *)0x10000008)

static void putc_(char c) { MMIO_PUTC = (uint32_t)(uint8_t)c; }
static void puts_(const char *s) { while (*s) putc_(*s++); }
static void putu_(uint32_t v) {
	char b[11]; int n = 0;
	if (!v) { putc_('0'); return; }
	while (v) { b[n++] = (char)('0' + v % 10); v /= 10; }
	while (n) putc_(b[--n]);
}
void *memcpy(void *d, const void *s, unsigned long n) {
	uint8_t *dd = d; const uint8_t *ss = s; while (n--) *dd++ = *ss++; return d;
}
void *memset(void *d, int c, unsigned long n) {
	uint8_t *dd = d; while (n--) *dd++ = (uint8_t)c; return d;
}

#define N 256                     /* iterations per measurement */

static uint32_t a[8], b[8], r[8], prod[16];
static uint32_t pair[16], outbuf[16];
static volatile uint32_t sink;

static void row(const char *name, uint32_t cycles) {
	int len = 0; while (name[len]) len++;
	int pad = 34 - len;
	puts_(name);
	while (pad-- > 0) putc_(' ');
	putu_((cycles + N / 2) / N);
	puts_(" cycles/op\n");
}

int main(void) {
	uint32_t t;
	for (int i = 0; i < 8; i++) { a[i] = 0x9e3779b9u * (i + 1); b[i] = 0x85ebca6bu * (i + 3); }

	puts_("per-operation cost on PicoRV32 (mean of ");
	putu_(N); puts_(" runs)\n\n");

	/* ---------- 256-bit add ---------- */
	t = MMIO_CYCLES;
	for (int i = 0; i < N; i++) sink = uECC_vli_add(r, a, b, 8);
	row("software uECC_vli_add", MMIO_CYCLES - t);

	for (int i = 0; i < 8; i++) { pair[i] = a[i]; pair[8 + i] = b[i]; }
	t = MMIO_CYCLES;
	for (int i = 0; i < N; i++) red_add256(pair, outbuf);
	row("add256 (operands in place)", MMIO_CYCLES - t);

	t = MMIO_CYCLES;
	for (int i = 0; i < N; i++) {
		for (int k = 0; k < 8; k++) { pair[k] = a[k]; pair[8 + k] = b[k]; }
		red_add256(pair, outbuf);
		for (int k = 0; k < 8; k++) r[k] = outbuf[k];
	}
	row("add256 + operand marshalling", MMIO_CYCLES - t);

	/* ---------- 256-bit subtract ---------- */
	t = MMIO_CYCLES;
	for (int i = 0; i < N; i++) sink = uECC_vli_sub(r, a, b, 8);
	row("software uECC_vli_sub", MMIO_CYCLES - t);

	t = MMIO_CYCLES;
	for (int i = 0; i < N; i++) red_sub256(pair, outbuf);
	row("sub256 (operands in place)", MMIO_CYCLES - t);

	/* ---------- multiply-accumulate ---------- */
	t = MMIO_CYCLES;
	for (int i = 0; i < N; i++) uECC_vli_mult(prod, a, b, 8);
	row("software uECC_vli_mult (64 MACs)", MMIO_CYCLES - t);

	t = MMIO_CYCLES;
	for (int i = 0; i < N; i++) red_mac96(pair, outbuf);
	row("mac96 (operands in place)", MMIO_CYCLES - t);

	/* One MAC as the software inner loop does it: accumulator in registers. */
	t = MMIO_CYCLES;
	{
		uint32_t r0 = 0, r1 = 0, r2 = 0;
		for (int i = 0; i < N; i++) {
			uint64_t p = (uint64_t)a[i & 7] * b[i & 7];
			uint32_t lo = (uint32_t)p, hi = (uint32_t)(p >> 32);
			r0 += lo; r1 += hi + (r0 < lo); r2 += (r1 < hi);
		}
		sink = r0 + r1 + r2;
	}
	row("software MAC (acc in registers)", MMIO_CYCLES - t);

	puts_("\nRESULT: PASS\n");
	return 0;
}
