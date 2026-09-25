
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>

static inline uint32_t nan_canon(uint32_t u) {
    uint32_t exp = (u >> 23) & 0xFFu;
    uint32_t frac = u & 0x7FFFFFu;
    uint32_t sign = u & 0x80000000u;
    
    uint32_t exp_nz = (exp | (0u - exp)) >> 31;
    uint32_t exp_zero_mask = (exp_nz ^ 1u) * 0xFFFFFFFFu;
    
    uint32_t frac_nz = (frac | (0u - frac)) >> 31;
    uint32_t frac_nz_mask = frac_nz * 0xFFFFFFFFu;
    
    uint32_t subnormal_mask = exp_zero_mask & frac_nz_mask;
    
    uint32_t exp_not_ff = exp ^ 0xFFu;
    uint32_t exp_not_ff_nz = (exp_not_ff | (0u - exp_not_ff)) >> 31;
    uint32_t exp_ff_mask = (exp_not_ff_nz ^ 1u) * 0xFFFFFFFFu;
    
    uint32_t nan_mask = exp_ff_mask & frac_nz_mask;
    
    uint32_t flushed = (u & ~subnormal_mask) | (sign & subnormal_mask);
    uint32_t res = (flushed & ~nan_mask) | (0x7FC00000u & nan_mask);
    
    return res;
}

static inline float ftz(float f) {
    uint32_t u;
    memcpy(&u, &f, 4);
    u = nan_canon(u);
    float res;
    memcpy(&res, &u, 4);
    return res;
}

void fdot2_custom_model(const uint32_t in[5], uint32_t out[5]) {
    float c, a0, a1, b0, b1;
    
    memcpy(&c,  &in[0], 4);
    memcpy(&a0, &in[1], 4);
    memcpy(&a1, &in[2], 4);
    memcpy(&b0, &in[3], 4);
    memcpy(&b1, &in[4], 4);
    
    c = ftz(c);
    a0 = ftz(a0);
    a1 = ftz(a1);
    b0 = ftz(b0);
    b1 = ftz(b1);
    
    float p0 = ftz(a0 * b0);
    float sum1 = ftz(c + p0);
    
    float p1 = ftz(a1 * b1);
    float sum2 = ftz(sum1 + p1);
    
    uint32_t res_u;
    memcpy(&res_u, &sum2, 4);
    res_u = nan_canon(res_u);
    
    out[0] = res_u;
    out[1] = 0;
    out[2] = 0;
    out[3] = 0;
    out[4] = 0;
}
/* ---- end model ---- */

#define WORDS 5
#define NVEC  1
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
    fdot2_custom_model(in, a);
    if (memcmp(in, shadow, WORDS * 4) != 0) {
        printf("FAIL mutates-input\n"); rc = 2; goto done;
    }
    for (w = 0; w < WORDS; w++) b[w] = POISON_B;
    fdot2_custom_model(in, b);
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
            fdot2_custom_model(in, out);
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
