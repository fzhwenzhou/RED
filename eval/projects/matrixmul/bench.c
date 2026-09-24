#include <stdint.h>
#define MMIO_PUTC (*(volatile uint32_t *)0x10000000)
#define MMIO_CYCLES (*(volatile uint32_t *)0x10000008)
#ifndef RED_EXT
#define RED_EXT 0
#endif
#define N 10
static float a[N][N], b[N][N], got[N][N], ref[N][N];
static void putc_(char c){MMIO_PUTC=(uint8_t)c;}
static void puts_(const char*s){while(*s)putc_(*s++);}
static void putu_(uint32_t v){char bfr[11];int n=0;if(!v){putc_('0');return;}while(v){bfr[n++]='0'+v%10;v/=10;}while(n)putc_(bfr[--n]);}
static void puthex_(uint32_t v){static const char h[]="0123456789abcdef";for(int i=7;i>=0;i--)putc_(h[(v>>(i*4))&15]);}
void *memcpy(void*d,const void*s,unsigned long n){uint8_t*dd=d;const uint8_t*ss=s;while(n--)*dd++=*ss++;return d;}
void *memset(void*d,int c,unsigned long n){uint8_t*dd=d;while(n--)*dd++=(uint8_t)c;return d;}
static uint32_t bits(float f){union{float f;uint32_t u;}x={.f=f};return x.u;}
static float from_bits(uint32_t u){union{float f;uint32_t u;}x={.u=u};return x.f;}
static inline void red_fdot2(const uint32_t in[5],uint32_t out[5]){
    register const uint32_t *ip __asm__("a0")=in;
    register uint32_t *op __asm__("a1")=out;
    uint32_t z;
    __asm__ __volatile__(".insn r 0x0b, 0, 6, %0, %1, %2":"=r"(z):"r"(ip),"r"(op):"memory");
}
static void software(float out[N][N]){
    for(int i=0;i<N;i++)for(int j=0;j<N;j++){
        float c=0.0f;
        for(int k=0;k<N;k++)c=c+a[i][k]*b[k][j];
        out[i][j]=c;
    }
}
static void accelerated(float out[N][N]){
    uint32_t in[5],o[5];
    for(int i=0;i<N;i++)for(int j=0;j<N;j++){
        float c=0.0f;
        for(int k=0;k<N;k+=2){
            in[0]=bits(a[i][k]);in[1]=bits(a[i][k+1]);
            in[2]=bits(b[k][j]);in[3]=bits(b[k+1][j]);in[4]=bits(c);
            red_fdot2(in,o);c=from_bits(o[0]);
        }
        out[i][j]=c;
    }
}
int main(void){
    for(int i=0;i<N;i++)for(int j=0;j<N;j++){
        a[i][j]=(float)((i+1)+(j+1))*0.01f;
        b[i][j]=(float)((i+1)*(j+1))*0.01f;
    }
    uint32_t t=MMIO_CYCLES;
#if RED_EXT
    accelerated(got);
#else
    software(got);
#endif
    uint32_t cycles=MMIO_CYCLES-t;
    software(ref);
    int bad=0;uint32_t signature=0;
    for(int i=0;i<N;i++)for(int j=0;j<N;j++){
        uint32_t g=bits(got[i][j]),r=bits(ref[i][j]);
        if(g!=r)bad++;
        signature=(signature<<1)|(signature>>31);signature^=g;
    }
    puts_("signature : ");puthex_(signature);putc_('\n');
    puts_("cycles TOTAL : ");putu_(cycles);putc_('\n');
    puts_(bad?"RESULT: FAIL\n":"RESULT: PASS\n");return bad?1:0;
}
