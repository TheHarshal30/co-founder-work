#include <stdio.h>

long long sum(long long x, long long y) { return x + y; }

int main(void) {
    long long x, y;
    printf("Enter two numbers: ");
    scanf("%lld %lld", &x, &y);
    printf("Sum = %lld\n", sum(x, y));
    return 0;
}
