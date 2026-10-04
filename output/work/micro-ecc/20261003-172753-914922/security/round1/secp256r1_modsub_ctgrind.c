
#include <stdint.h>
#include <string.h>
#include <valgrind/memcheck.h>

/* ---- designer-supplied C reference model ---- */
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

/* ---- end model ---- */

#define WORDS 16

/* The result is consumed through a volatile sink rather than through the exit
   status, so the process's exit code carries memcheck's verdict and nothing
   else: 0 is clean, 97 is "memcheck found errors", anything else means
   memcheck itself could not run. */
static volatile uint32_t sink;

int main(void) {
    uint32_t in[WORDS], out[WORDS];
    int w;
    for (w = 0; w < WORDS; w++) in[w] = 0x9e3779b9u * (uint32_t)(w + 1);
    memset(out, 0, sizeof out);
    VALGRIND_MAKE_MEM_UNDEFINED(in, sizeof in);
    secp256r1_modsub_model(in, out);
    VALGRIND_MAKE_MEM_DEFINED(out, sizeof out);
    for (w = 0; w < WORDS; w++) sink = out[w];
    return 0;
}
