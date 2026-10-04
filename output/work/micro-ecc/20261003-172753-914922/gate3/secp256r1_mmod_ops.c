
#include <stdint.h>
#include <string.h>
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

int main(void) {
    uint32_t in[16], out[16];
    for (int i = 0; i < 16; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    secp256r1_mmod_model(in, out);
    return (int)(out[0] & 1);
}
