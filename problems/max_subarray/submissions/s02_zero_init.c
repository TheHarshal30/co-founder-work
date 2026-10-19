#include <stdio.h>

/* Kadane's algorithm */
long long kadane(int n)
{
    long long best = 0, cur = 0, x;
    for (int i = 0; i < n; i++) {
        scanf("%lld", &x);
        cur = cur + x;
        if (cur < 0)
            cur = 0;
        if (cur > best)
            best = cur;
    }
    return best;
}

int main(void)
{
    int n;
    scanf("%d", &n);
    printf("%lld\n", kadane(n));
    return 0;
}
