#include <stdio.h>

int main() {
    int num1, num2;
    
    // Read the input integers
    if (scanf("%d %d", &num1, &num2) != 2) {
        printf("Invalid input\n");
        return 1;
    }
    
    // Calculate the sum
    long long result = (long long)num1 + num2;

    // Check for overflow
    if ((result > INT_MAX) || (result < INT_MIN)) {
        printf("Overflow detected: %lld\n", result);
    } else {
        printf("%d\n", (int)result);
    }
    
    return 0;
}
