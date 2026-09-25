
#include <stdint.h>
#include <string.h>
#include <stdint.h>
#include <string.h>

void fmatmul2_custom_model(const uint32_t in[8], uint32_t out[8]) {
    float A[4], B[4], C[4];
    memcpy(A, &in[0], 16);
    memcpy(B, &in[4], 16);
    
    C[0] = 0.0f;
    C[0] = C[0] + A[0]*B[0];
    C[0] = C[0] + A[1]*B[2];
    
    C[1] = 0.0f;
    C[1] = C[1] + A[0]*B[1];
    C[1] = C[1] + A[1]*B[3];
    
    C[2] = 0.0f;
    C[2] = C[2] + A[2]*B[0];
    C[2] = C[2] + A[3]*B[2];
    
    C[3] = 0.0f;
    C[3] = C[3] + A[2]*B[1];
    C[3] = C[3] + A[3]*B[3];
    
    memcpy(&out[0], C, 16);
    out[4] = 0;
    out[5] = 0;
    out[6] = 0;
    out[7] = 0;
}
int main(void) {
    uint32_t in[8], out[8];
    for (int i = 0; i < 8; i++) in[i] = 0x9e3779b9u * (uint32_t)(i + 1);
    memset(out, 0, sizeof out);
    fmatmul2_custom_model(in, out);
    return (int)(out[0] & 1);
}
