
#include <stdint.h>
#include <string.h>
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
int main(void) {
    uint32_t in[5], out[5];
    for (int i = 0; i < 5; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    fdot2_custom_model(in, out);
    return (int)(out[0] & 1);
}
