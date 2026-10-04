
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>
/* mnemonic secp256r1.mmod -> secp256r1_mmod_model; in[0..15]=512-bit X, out[0..7]=X mod p256, out[8..15]=0 */
static const uint32_t MM_P256[8] = {0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFFu, 0x00000000u,
                                    0x00000000u, 0x00000000u, 0x00000001u, 0xFFFFFFFFu};
/* FIPS 186-4 D.2.3 terms as source-word indices into c[0..16] (index 16 = constant zero), word 0 first.
   Pass order: s1, s1, s2, s2, s3, s4, d1, d2, d3, d4 (s1, s2 added twice = 2*s1, 2*s2). */
static const unsigned char MM_TERM[10][8] = {
    {16, 16, 16, 11, 12, 13, 14, 15},
    {16, 16, 16, 11, 12, 13, 14, 15},
    {16, 16, 16, 12, 13, 14, 15, 16},
    {16, 16, 16, 12, 13, 14, 15, 16},
    { 8,  9, 10, 16, 16, 16, 14, 15},
    { 9, 10, 11, 13, 14, 15, 13,  8},
    {11, 12, 13, 16, 16, 16,  8, 10},
    {12, 13, 14, 15, 16, 16,  9, 11},
    {13, 14, 15,  8,  9, 10, 16, 12},
    {14, 15, 16,  9, 10, 11, 16, 13}};
static const uint32_t MM_NEG[10] = {0u, 0u, 0u, 0u, 0u, 0u, 1u, 1u, 1u, 1u};
void secp256r1_mmod_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t c[17];
    uint32_t r[8], a[8], d[8];
    uint32_t carry; /* signed top word t, two's complement */
    int i, k;
    for (i = 0; i < 16; i++) c[i] = in[i];
    c[16] = 0;
    for (i = 0; i < 8; i++) r[i] = c[i];
    carry = 0;
    /* ten 256-bit add/sub passes: R = R + (term XOR sm) + neg; carry = carry + cout - neg */
    for (k = 0; k < 10; k++) {
        uint32_t neg = MM_NEG[k];
        uint32_t sm = (uint32_t)0 - neg;
        uint64_t s = neg;
        for (i = 0; i < 8; i++) {
            s = s + (uint64_t)r[i] + (uint64_t)(c[MM_TERM[k][i]] ^ sm);
            r[i] = (uint32_t)s;
            s >>= 32;
        }
        carry = carry + (uint32_t)s - neg;
    }
    /* X == t*2^256 + R (mod p), t in [-4, 6]. Eight constant-time correction steps:
       if t < 0 add p; else if t != 0 or R >= p subtract p; else keep. */
    for (k = 0; k < 8; k++) {
        uint64_t sa = 0, sd = 0;
        uint32_t b, ca, isneg, nz, dosub, doadd, ms, ma;
        for (i = 0; i < 8; i++) {
            sa = sa + (uint64_t)r[i] + (uint64_t)MM_P256[i];
            a[i] = (uint32_t)sa;
            sa >>= 32;
            sd = (uint64_t)r[i] - (uint64_t)MM_P256[i] - sd;
            d[i] = (uint32_t)sd;
            sd = (sd >> 32) & 1u;
        }
        ca = (uint32_t)sa;
        b = (uint32_t)sd;
        isneg = carry >> 31;
        nz = (carry | ((uint32_t)0 - carry)) >> 31;
        doadd = isneg;
        dosub = (1u - isneg) & (nz | (1u - b));
        ma = (uint32_t)0 - doadd;
        ms = (uint32_t)0 - dosub;
        for (i = 0; i < 8; i++)
            r[i] = (a[i] & ma) | (d[i] & ms) | (r[i] & ~(ma | ms));
        carry = carry + (ca & doadd) - (b & dosub);
    }
    for (i = 0; i < 8; i++) out[i] = r[i];
    for (i = 8; i < 16; i++) out[i] = 0;
}

/* ---- end model ---- */

#define WORDS 16
#define NVEC  512
#define POISON_A 0xA5A5A5A5u
#define POISON_B 0x5C5C5C5Cu

static uint32_t xs32(uint32_t *s) {
    uint32_t x = *s;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return *s = x;
}

/* Adversarial patterns first: saturating values, one bit at every word
   boundary, and carry ripples that reach exactly as far as each word. Then
   pseudo-random fill for the rest. */
static void fill(uint32_t *in, long idx, uint32_t *seed) {
    static const uint32_t pat[] = { 0u, 0xFFFFFFFFu, 1u, 0x80000000u,
                                    0xAAAAAAAAu, 0x55555555u };
    const long npat = (long)(sizeof pat / sizeof pat[0]);
    int w;
    if (idx < npat) {
        for (w = 0; w < WORDS; w++) in[w] = pat[idx];
        return;
    }
    idx -= npat;
    if (idx < (long)WORDS * 3) {
        long w0 = idx / 3; int k = (int)(idx % 3);
        for (w = 0; w < WORDS; w++) in[w] = 0u;
        in[w0] = (k == 0) ? 1u : (k == 1) ? 0x80000000u : 0xFFFFFFFFu;
        return;
    }
    idx -= (long)WORDS * 3;
    if (idx < (long)WORDS) {
        for (w = 0; w < WORDS; w++) in[w] = ((long)w <= idx) ? 0xFFFFFFFFu : 0u;
        return;
    }
    for (w = 0; w < WORDS; w++) in[w] = xs32(seed);
}

static void show(const char *label, const uint32_t *v) {
    int w;
    printf("%s ", label);
    for (w = 0; w < WORDS; w++) printf("%08x", v[w]);
    printf("\n");
}

/* One vector, twice, against two poisons. Returns 0 when clean. */
static int trial(const uint32_t *src, int report) {
    uint32_t *in     = (uint32_t *)malloc(WORDS * 4);
    uint32_t *shadow = (uint32_t *)malloc(WORDS * 4);
    uint32_t *a      = (uint32_t *)malloc(WORDS * 4);
    uint32_t *b      = (uint32_t *)malloc(WORDS * 4);
    int rc = 0, w, unwritten = -1, impure = -1;
    if (!in || !shadow || !a || !b) return 9;
    memcpy(in, src, WORDS * 4);
    memcpy(shadow, src, WORDS * 4);
    for (w = 0; w < WORDS; w++) a[w] = POISON_A;
    secp256r1_mmod_model(in, a);
    if (memcmp(in, shadow, WORDS * 4) != 0) {
        printf("FAIL mutates-input\n"); rc = 2; goto done;
    }
    for (w = 0; w < WORDS; w++) b[w] = POISON_B;
    secp256r1_mmod_model(in, b);
    if (memcmp(in, shadow, WORDS * 4) != 0) {
        printf("FAIL mutates-input\n"); rc = 2; goto done;
    }
    for (w = 0; w < WORDS; w++) {
        if (a[w] == b[w]) continue;
        if (a[w] == POISON_A && b[w] == POISON_B) {
            if (unwritten < 0) unwritten = w;
        } else if (impure < 0) {
            impure = w;
        }
    }
    if (impure >= 0) {
        printf("FAIL impure word=%d %08x vs %08x\n", impure,
               a[impure], b[impure]);
        rc = 3; goto done;
    }
    if (unwritten >= 0) {
        printf("FAIL unwritten-output word=%d of %d\n", unwritten, WORDS);
        rc = 4; goto done;
    }
    if (report) show("out", a);
done:
    free(in); free(shadow); free(a); free(b);
    return rc;
}

int main(int argc, char **argv) {
    uint32_t in[WORDS];
    uint32_t seed = 0xC0FFEEu;
    long i;
    int w, rc;
    if (argc > 1) {                      /* probe: one caller-supplied vector */
        if (strlen(argv[1]) != (size_t)(8 * WORDS)) {
            printf("FAIL bad-vector expected %d hex digits\n", 8 * WORDS);
            return 5;
        }
        for (w = 0; w < WORDS; w++) {
            char buf[9]; char *end;
            memcpy(buf, argv[1] + 8 * w, 8); buf[8] = 0;
            in[w] = (uint32_t)strtoul(buf, &end, 16);
            if (*end) { printf("FAIL bad-vector\n"); return 5; }
        }
        if (argc > 2) {                  /* timing mode: exactly one call */
            uint32_t out[WORDS];
            for (w = 0; w < WORDS; w++) out[w] = 0u;
            secp256r1_mmod_model(in, out);
            return (int)(out[0] & 1u);
        }
        rc = trial(in, 1);
        if (rc) show("vector", in);
        return rc;
    }
    for (i = 0; i < NVEC; i++) {
        fill(in, i, &seed);
        rc = trial(in, 0);
        if (rc) { show("vector", in); return rc; }
    }
    printf("PASS %d vectors\n", NVEC);
    return 0;
}
