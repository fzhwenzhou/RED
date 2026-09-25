
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

void fdot2_custom_model(const uint32_t in[5], uint32_t out[5]) {
    float a0, a1, b0, b1, c;
    uint32_t tmp;

    tmp = ftz_u32(in[0]); memcpy(&a0, &tmp, 4);
    tmp = ftz_u32(in[1]); memcpy(&a1, &tmp, 4);
    tmp = ftz_u32(in[2]); memcpy(&b0, &tmp, 4);
    tmp = ftz_u32(in[3]); memcpy(&b1, &tmp, 4);
    tmp = ftz_u32(in[4]); memcpy(&c, &tmp, 4);

    float p0 = ftz_f32(a0 * b0);
    float p1 = ftz_f32(a1 * b1);
    float s0 = ftz_f32(c + p0);
    float res = ftz_f32(s0 + p1);

    memcpy(&out[0], &res, 4);
    out[0] = ftz_u32(out[0]);
    out[1] = 0;
    out[2] = 0;
    out[3] = 0;
    out[4] = 0;
}
int main(void) {
    uint32_t in[5], out[5];
    for (int i = 0; i < 5; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    fdot2_custom_model(in, out);
    return (int)(out[0] & 1);
}
