
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>

void secp256r1_mmod_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t result[8];
    uint32_t tmp[8];
    int64_t carry = 0;
    uint64_t c;
    const uint32_t p[8] = {0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0x00000000, 
                           0x00000000, 0x00000000, 0x00000001, 0xFFFFFFFF};

    for (int i = 0; i < 8; ++i) result[i] = in[i];

    #define DO_DOUBLE_ADD() do { \
        c = 0; \
        for(int i = 0; i < 8; ++i) { \
            c += (uint64_t)tmp[i] + tmp[i]; \
            tmp[i] = (uint32_t)c; \
            c >>= 32; \
        } \
        carry += c; \
    } while(0)

    #define DO_ADD() do { \
        c = 0; \
        for(int i = 0; i < 8; ++i) { \
            c += (uint64_t)result[i] + tmp[i]; \
            result[i] = (uint32_t)c; \
            c >>= 32; \
        } \
        carry += c; \
    } while(0)

    #define DO_SUB() do { \
        c = 0; \
        for(int i = 0; i < 8; ++i) { \
            c = (uint64_t)result[i] - tmp[i] - c; \
            result[i] = (uint32_t)c; \
            c = (c >> 32) & 1; \
        } \
        carry -= c; \
    } while(0)

    tmp[0]=0; tmp[1]=0; tmp[2]=0; tmp[3]=in[11]; tmp[4]=in[12]; tmp[5]=in[13]; tmp[6]=in[14]; tmp[7]=in[15];
    DO_DOUBLE_ADD(); DO_ADD();

    tmp[0]=0; tmp[1]=0; tmp[2]=0; tmp[3]=in[12]; tmp[4]=in[13]; tmp[5]=in[14]; tmp[6]=in[15]; tmp[7]=0;
    DO_DOUBLE_ADD(); DO_ADD();

    tmp[0]=in[8]; tmp[1]=in[9]; tmp[2]=in[10]; tmp[3]=0; tmp[4]=0; tmp[5]=0; tmp[6]=in[14]; tmp[7]=in[15];
    DO_ADD();

    tmp[0]=in[9]; tmp[1]=in[10]; tmp[2]=in[11]; tmp[3]=in[13]; tmp[4]=in[14]; tmp[5]=in[15]; tmp[6]=in[13]; tmp[7]=in[8];
    DO_ADD();

    tmp[0]=in[11]; tmp[1]=in[12]; tmp[2]=in[13]; tmp[3]=0; tmp[4]=0; tmp[5]=0; tmp[6]=in[8]; tmp[7]=in[10];
    DO_SUB();

    tmp[0]=in[12]; tmp[1]=in[13]; tmp[2]=in[14]; tmp[3]=in[15]; tmp[4]=0; tmp[5]=0; tmp[6]=in[9]; tmp[7]=in[11];
    DO_SUB();

    tmp[0]=in[13]; tmp[1]=in[14]; tmp[2]=in[15]; tmp[3]=in[8]; tmp[4]=in[9]; tmp[5]=in[10]; tmp[6]=0; tmp[7]=in[12];
    DO_SUB();

    tmp[0]=in[14]; tmp[1]=in[15]; tmp[2]=0; tmp[3]=in[9]; tmp[4]=in[10]; tmp[5]=in[11]; tmp[6]=0; tmp[7]=in[13];
    DO_SUB();

    for (int j = 0; j < 8; j++) {
        uint32_t mask = (uint32_t)0 - ((uint64_t)carry >> 63);
        c = 0;
        for (int i = 0; i < 8; i++) {
            c += (uint64_t)result[i] + (p[i] & mask);
            tmp[i] = (uint32_t)c;
            c >>= 32;
        }
        for (int i = 0; i < 8; i++) result[i] = tmp[i];
        carry += (c & mask); 
    }

    for (int j = 0; j < 16; j++) {
        c = 0;
        for (int i = 0; i < 8; i++) {
            c = (uint64_t)result[i] - p[i] - c;
            tmp[i] = (uint32_t)c;
            c = (c >> 32) & 1;
        }
        uint32_t cond = (carry > 0) | ((carry == 0) & (c == 0));
        uint32_t mask = (uint32_t)0 - cond;
        for (int i = 0; i < 8; i++) {
            result[i] = (result[i] & ~mask) | (tmp[i] & mask);
        }
        carry -= (c & mask);
    }

    for (int i = 0; i < 8; ++i) out[i] = result[i];
    for (int i = 8; i < 16; ++i) out[i] = 0;
}
/* ---- end model ---- */

#define WORDS 16
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
