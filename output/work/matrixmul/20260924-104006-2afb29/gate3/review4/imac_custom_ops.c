
#include <stdint.h>
#include <string.h>
#include <stdint.h>
#include <string.h>

void imac_custom_model(const uint32_t in[3], uint32_t out[3]) {
    out[0] = in[2] + in[0] * in[1];
    out[1] = 0;
    out[2] = 0;
}
int main(void) {
    uint32_t in[3], out[3];
    for (int i = 0; i < 3; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    imac_custom_model(in, out);
    return (int)(out[0] & 1);
}
