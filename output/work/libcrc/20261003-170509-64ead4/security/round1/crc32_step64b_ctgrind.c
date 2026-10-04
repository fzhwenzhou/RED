
#include <stdint.h>
#include <string.h>
#include <valgrind/memcheck.h>

/* ---- designer-supplied C reference model ---- */
#include <stdint.h>
#include <string.h>
/* mnemonic "crc32.step64b" -> crc32_step64b_model.
   in[0] = running CRC-32 state; in[1..16] = 64 data bytes, little-endian:
   byte k = (in[1 + k/4] >> (8*(k%4))) & 0xFF. out[0] = new state, out[1..16] = 0. */
void crc32_step64b_model(const uint32_t in[17], uint32_t out[17]) {
    uint32_t crc = in[0];
    int i, j, b;
    for (i = 0; i < 16; i++) {
        uint32_t w = in[1 + i];
        for (j = 0; j < 4; j++) {
            crc ^= (w >> (8 * j)) & 0xFFu;
            for (b = 0; b < 8; b++) {
                uint32_t m = (uint32_t)0 - (crc & 1u);
                crc = (crc >> 1) ^ (0xEDB88320u & m);
            }
        }
    }
    out[0] = crc;
    for (i = 1; i < 17; i++) out[i] = 0;
}

/* ---- end model ---- */

#define WORDS 17

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
    crc32_step64b_model(in, out);
    VALGRIND_MAKE_MEM_DEFINED(out, sizeof out);
    for (w = 0; w < WORDS; w++) sink = out[w];
    return 0;
}
