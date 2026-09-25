#include "matrixmul.h"

int main(void) {
    Matrix A, B, C;
    float sum = 0.0f;

    setRowsColumns(10, 10, &A);
    setRowsColumns(10, 10, &B);

    for (int i = 1; i <= 10; i++) {
        for (int j = 1; j <= 10; j++) {
            setElement(i, j, (float)(i + j) * 0.1f, &A);
            setElement(i, j, (float)(i * j) * 0.1f, &B);
        }
    }

    for (int iter = 0; iter < 10000; iter++) {
        multiply(&A, &B, &C);
        
        // Feedback a small change to prevent optimization
        float c_val = getElement(1, 1, &C);
        setElement(1, 1, c_val * 0.00001f, &A);
        
        sum += c_val;
    }

    if (sum < -1000.0f) {
        return 1;
    }

    return 0;
}
