/* Per-operation cost for the pinned secp256r1.mult extension. */
#include <stdint.h>
#include "uECC.h"
#include "uECC_vli.h"
#include "red_ext.h"
#define MMIO_PUTC   (*(volatile uint32_t *)0x10000000)
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
    uint8_t *dd=d; const uint8_t *ss=s; while (n--) *dd++=*ss++; return d;
}
void *memset(void *d, int c, unsigned long n) {
    uint8_t *dd=d; while (n--) *dd++=(uint8_t)c; return d;
}
#define N 256
static uint32_t a[8], b[8], prod[16], pair[16], outbuf[16];
static void row(const char *name, uint32_t cycles) {
    int len=0; while (name[len]) len++;
    puts_(name); while (len++ < 38) putc_(' ');
    putu_((cycles + N/2) / N); puts_(" cycles/op\n");
}
int main(void) {
    uint32_t t;
    for (int i=0; i<8; i++) {
        a[i] = 0x9e3779b9u * (i+1); b[i] = 0x85ebca6bu * (i+3);
        pair[i] = a[i]; pair[8+i] = b[i];
    }
    puts_("per-operation cost on PicoRV32 (mean of 256 runs)\n\n");
    t = MMIO_CYCLES;
    for (int i=0; i<N; i++) uECC_vli_mult(prod, a, b, 8);
    row("uECC_vli_mult (including call)", MMIO_CYCLES-t);
    t = MMIO_CYCLES;
    for (int i=0; i<N; i++) red_mult256(pair, outbuf);
    row("mult256 (operands in place)", MMIO_CYCLES-t);
    t = MMIO_CYCLES;
    for (int i=0; i<N; i++) {
        for (int k=0; k<8; k++) { pair[k]=a[k]; pair[8+k]=b[k]; }
        red_mult256(pair, outbuf);
    }
    row("mult256 + operand marshalling", MMIO_CYCLES-t);
    for (int i=0; i<16; i++) if (prod[i] != outbuf[i]) {
        puts_("RESULT: FAIL\n"); return 1;
    }
    puts_("\nRESULT: PASS\n"); return 0;
}
