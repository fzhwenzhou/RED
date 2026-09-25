
#include <stdint.h>
#include <string.h>
#include <stdint.h>
#include <string.h>

static uint32_t ftz_u32(uint32_t bits) {
    uint32_t exp = (bits >> 23) & 0xFF;
    uint32_t exp_is_zero = (exp - 1) >> 31;
    uint32_t mask = 0xFFFFFFFF ^ (exp_is_zero * 0x7FFFFFFF);
    return bits & mask;
}

static float ftz_f32(float x) {
    uint32_t bits;
    memcpy(&bits, &x, 4);
    bits = ftz_u32(bits);
    memcpy(&x, &bits, 4);
    return x;
}

static uint32_t canon_nan(float x) {
    uint32_t bits;
    memcpy(&bits, &x, 4);
    uint32_t exp = (bits >> 23) & 0xFF;
    uint32_t frac = bits & 0x7FFFFF;
    if (exp == 0xFF && frac != 0) {
        return 0x7FC00000;
    }
    return bits;
}

void fmac_custom_model(const uint32_t in[3], uint32_t out[3]) {
    float a, b, c;
    uint32_t tmp;

    tmp = ftz_u32(in[0]); memcpy(&a, &tmp, 4);
    tmp = ftz_u32(in[1]); memcpy(&b, &tmp, 4);
    tmp = ftz_u32(in[2]); memcpy(&c, &tmp, 4);

    float p = ftz_f32(a * b);
    uint32_t p_bits = canon_nan(p);
    memcpy(&p, &p_bits, 4);

    float s = ftz_f32(c + p);
    uint32_t s_bits = canon_nan(s);

    out[0] = ftz_u32(s_bits);
    out[1] = 0;
    out[2] = 0;
}
int main(void) {
    uint32_t in[3], out[3];
    for (int i = 0; i < 3; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    fmac_custom_model(in, out);
    return (int)(out[0] & 1);
}
