#include <cstdint>
#include <cstring>
static uint32_t canonical(uint32_t u) {
    return ((u & 0x7f800000u) == 0x7f800000u && (u & 0x007fffffu))
        ? 0x7fc00000u : u;
}
extern "C" int f32_mul_dpi(int xi, int yi) {
    uint32_t x=(uint32_t)xi,y=(uint32_t)yi,z; float a,b;
    std::memcpy(&a,&x,4); std::memcpy(&b,&y,4);
    volatile float r=a*b; float rr=r; std::memcpy(&z,&rr,4);
    return (int)canonical(z);
}
extern "C" int f32_add_dpi(int xi, int yi) {
    uint32_t x=(uint32_t)xi,y=(uint32_t)yi,z; float a,b;
    std::memcpy(&a,&x,4); std::memcpy(&b,&y,4);
    volatile float r=a+b; float rr=r; std::memcpy(&z,&rr,4);
    return (int)canonical(z);
}
