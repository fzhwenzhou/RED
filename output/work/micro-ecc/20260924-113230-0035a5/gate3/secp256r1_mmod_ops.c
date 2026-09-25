
#include <stdint.h>
#include <string.h>
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
int main(void) {
    uint32_t in[16], out[16];
    for (int i = 0; i < 16; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    secp256r1_mmod_model(in, out);
    return (int)(out[0] & 1);
}
