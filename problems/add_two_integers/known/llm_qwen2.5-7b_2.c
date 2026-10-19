#include <stdio.h>
#include <stdlib.h>

int main() {
    int num1, num2;
    
    // Read two integers separated by whitespace
    if (scanf("%d %d", &num1, &num2) != 2) {
        fprintf(stderr, "Error reading input.\n");
        return 1;
    }
    
    // Calculate the sum
    int sum = num1 + num2;
    
    // Print the sum followed by a newline
    printf("%d\n", sum);
    
    return 0;
}
