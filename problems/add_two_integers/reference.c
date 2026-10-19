/* Reference solution: exact sum, computed in 64 bits. */
#include <stdio.h>

int main(void)
{
    long long a, b;
    if (scanf("%lld %lld", &a, &b) != 2)
        return 1;
    printf("%lld\n", a + b);
    return 0;
}
