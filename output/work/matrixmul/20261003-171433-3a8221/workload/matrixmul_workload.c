#include <stdio.h>
#include <stdint.h>
#include "matrixmul.h"

#define N 10
#define ITERS 30000

int main(void)
{
    static Matrix A, B, C;
    double checksum = 0.0;

    setRowsColumns(N, N, &A);
    setRowsColumns(N, N, &B);
    for (int i = 1; i <= N; i++) {
        for (int j = 1; j <= N; j++) {
            setElement(i, j, (float)((i * 7 + j * 3) % 11) * 0.125f - 0.5f, &A);
            setElement(i, j, (float)((i * 5 + j * 9) % 13) * 0.0625f + 0.25f, &B);
        }
    }

    for (int it = 0; it < ITERS; it++) {
        /* perturb one element of A and B so each multiply sees fresh input */
        unsigned char r = (unsigned char)(it % N + 1);
        unsigned char c = (unsigned char)((it / N) % N + 1);
        setElement(r, c, (float)(it % 17) * 0.03125f, &A);
        setElement(c, r, (float)(it % 19) * 0.015625f, &B);

        if (multiply(&A, &B, &C) != 0)
            return 1;

        checksum += getElement(r, c, &C) + getElement((unsigned char)(N + 1 - r), c, &C);
    }

    printf("checksum %.6f\n", checksum);
    return 0;
}
