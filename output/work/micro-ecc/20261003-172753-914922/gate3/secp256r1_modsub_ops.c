
#include <stdint.h>
#include <string.h>
#include <stdint.h>
#include <string.h>
/* mnemonic secp256r1.modsub -> secp256r1_modsub_model; in[0..7]=L, in[8..15]=R, out[0..7]=(L-R) mod p256, out[8..15]=0 */
static const uint32_t MS_P256[8] = {0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFFu, 0x00000000u,
                                    0x00000000u, 0x00000000u, 0x00000001u, 0xFFFFFFFFu};
void secp256r1_modsub_model(const uint32_t in[16], uint32_t out[16]) {
    uint32_t d[8];
    uint64_t b = 0, c = 0;
    uint32_t m;
    int i;
    for (i = 0; i < 8; i++) {
        uint64_t s = (uint64_t)in[i] - (uint64_t)in[8 + i] - b;
        d[i] = (uint32_t)s;
        b = (s >> 32) & 1u;
    }
    m = (uint32_t)0 - (uint32_t)b;
    for (i = 0; i < 8; i++) {
        uint64_t s = (uint64_t)d[i] + (uint64_t)(MS_P256[i] & m) + c;
        out[i] = (uint32_t)s;
        c = s >> 32;
    }
    for (i = 8; i < 16; i++) out[i] = 0;
}

int main(void) {
    uint32_t in[16], out[16];
    for (int i = 0; i < 16; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    secp256r1_modsub_model(in, out);
    return (int)(out[0] & 1);
}
