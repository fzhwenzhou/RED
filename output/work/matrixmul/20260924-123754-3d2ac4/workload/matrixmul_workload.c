#include "matrixmul.h"

int main(void) {
    Matrix A, B, C;
    setRowsColumns(10, 10, &A);
    setRowsColumns(10, 10, &B);

    for (int i = 1; i <= 10; i++) {
        for (int j = 1; j <= 10; j++) {
            setElement(i, j, (float)(i + j), &A);
            setElement(i, j, (float)(i - j), &B);
        }
    }

    float checksum = 0.0f;
    for (int iter = 0; iter < 10000; iter++) {
        setElement(1, 1, (float)(iter % 10), &A);
        multiply(&A, &B, &C);
        for (int i = 1; i <= 10; i++) {
            checksum += getElement(i, i, &C);
        }
    }

    if (checksum == 1.23456f) {
        return 1;
    }
    return 0;
}
