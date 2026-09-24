#include <stdint.h>
#include <stddef.h>
#include "checksum.h"
#define MMIO_PUTC (*(volatile uint32_t *)0x10000000)
#define MMIO_CYCLES (*(volatile uint32_t *)0x10000008)
#ifndef RED_EXT
#define RED_EXT 0
#endif
#define SIZE 4096
static uint8_t data[SIZE];
static void putc_(char c){MMIO_PUTC=(uint8_t)c;}
static void puts_(const char*s){while(*s)putc_(*s++);}
static void putu_(uint32_t v){char b[11];int n=0;if(!v){putc_('0');return;}while(v){b[n++]='0'+v%10;v/=10;}while(n)putc_(b[--n]);}
static void puthex_(uint32_t v){static const char h[]="0123456789abcdef";for(int i=7;i>=0;i--)putc_(h[(v>>(i*4))&15]);}
void *memcpy(void*d,const void*s,unsigned long n){uint8_t*dd=d;const uint8_t*ss=s;while(n--)*dd++=*ss++;return d;}
void *memset(void*d,int c,unsigned long n){uint8_t*dd=d;while(n--)*dd++=(uint8_t)c;return d;}
static uint16_t ref16(const uint8_t*p,size_t n,uint16_t c){while(n--){c^=*p++;for(int j=0;j<8;j++)c=(c>>1)^(0xA001u&(uint16_t)-(c&1));}return c;}
static uint32_t ref32(const uint8_t*p,size_t n,uint32_t c){while(n--){c^=*p++;for(int j=0;j<8;j++)c=(c>>1)^(0xEDB88320u&(uint32_t)-(c&1));}return c;}
static uint64_t ref64(const uint8_t*p,size_t n,uint64_t c){while(n--){c^=(uint64_t)*p++<<56;for(int j=0;j<8;j++)c=(c<<1)^(0x42F0E1EBA9EA3693ull&(uint64_t)-(c>>63));}return c;}
#if RED_EXT
static inline void red_crc(int f3,const uint32_t*in,uint32_t*out){
    register const uint32_t *ip __asm__("a0")=in;register uint32_t*op __asm__("a1")=out;uint32_t z;
    if(f3==0)__asm__ __volatile__(".insn r 0x7b, 0, 0, %0, %1, %2":"=r"(z):"r"(ip),"r"(op):"memory");
    else if(f3==1)__asm__ __volatile__(".insn r 0x7b, 1, 0, %0, %1, %2":"=r"(z):"r"(ip),"r"(op):"memory");
    else __asm__ __volatile__(".insn r 0x7b, 2, 0, %0, %1, %2":"=r"(z):"r"(ip),"r"(op):"memory");
}
static void pack32(uint32_t*out,const uint8_t*p){for(int i=0;i<8;i++)out[i]=(uint32_t)p[4*i]|(uint32_t)p[4*i+1]<<8|(uint32_t)p[4*i+2]<<16|(uint32_t)p[4*i+3]<<24;}
uint16_t crc_16(const unsigned char*p,size_t n){uint16_t c=0;uint32_t in[9],out[9];while(n>=32){in[0]=c;pack32(in+1,p);red_crc(1,in,out);c=out[0];p+=32;n-=32;}return ref16(p,n,c);}
uint32_t crc_32(const unsigned char*p,size_t n){uint32_t c=0xffffffffu,in[9],out[9];while(n>=32){in[0]=c;pack32(in+1,p);red_crc(2,in,out);c=out[0];p+=32;n-=32;}return ref32(p,n,c)^0xffffffffu;}
uint64_t crc_64_ecma(const unsigned char*p,size_t n){uint64_t c=0;uint32_t in[10],out[10];while(n>=32){in[0]=c;in[1]=c>>32;pack32(in+2,p);red_crc(0,in,out);c=(uint64_t)out[1]<<32|out[0];p+=32;n-=32;}return ref64(p,n,c);}
#endif
int main(void){
    for(int i=0;i<SIZE;i++)data[i]=(uint8_t)((i*137+7)&255);
    (void)crc_16(data,0); /* exclude one-time table initialization */
    uint32_t t=MMIO_CYCLES;
    uint32_t c32=crc_32(data,SIZE);uint64_t c64=crc_64_ecma(data,SIZE);uint16_t c16=crc_16(data,SIZE);
    uint32_t cycles=MMIO_CYCLES-t;
    int bad=c32!=(ref32(data,SIZE,0xffffffffu)^0xffffffffu)||c64!=ref64(data,SIZE,0)||c16!=ref16(data,SIZE,0);
    puts_("signature : ");puthex_(c32);puthex_(c64>>32);puthex_(c64);puthex_(c16);putc_('\n');
    puts_("cycles TOTAL : ");putu_(cycles);putc_('\n');
    puts_(bad?"RESULT: FAIL\n":"RESULT: PASS\n");return bad?1:0;
}
