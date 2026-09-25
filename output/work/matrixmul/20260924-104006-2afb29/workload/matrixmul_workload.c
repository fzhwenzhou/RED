#include <stdio.h>
#include <stdint.h>
#include "matrixmul.h"

int main(void) {
    Matrix A, B, C;
    
    setRowsColumns(10, 10, &A);
    setRowsColumns(10, 10, &B);
    
    for (int i = 1; i <= 10; i++) {
        for (int j = 1; j <= 10; j++) {
            setElement(i, j, (float)(i + j), &A);
            setElement(i, j, (float)(i * j), &B);
        }
    }
    
    float checksum = 0.0f;
    
    for (int iter = 0; iter < 15000; iter++) {
        setElement(1, 1, (float)(iter % 100), &A);
        
        if (multiply(&A, &B, &C) == 0) {
            for (int i = 1; i <= 10; i++) {
                for (int j = 1; j <= 10; j++) {
                    checksum += getElement(i, j, &C);
                }
            }
        }
    }
    
    printf("Checksum: %f\n", checksum);
    
    return 0;
}
