#include <stdio.h>
#include <stdlib.h>

int main() {
    int x, y;

    // Read two integers separated by whitespace
    if (scanf("%d %d", &x, &y) != 2) {
        fprintf(stderr, "Error reading input.\n");
        return 1;
    }

    // Calculate the sum
    int total = x + y;

    // Print the sum followed by a newline
    printf("%d\n", total);

    return 0;
}
