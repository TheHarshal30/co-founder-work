/* Reference solution: Kadane's algorithm in 64-bit arithmetic, O(n). */
#include <stdio.h>

int main(void)
{
    int n;
    if (scanf("%d", &n) != 1 || n < 1)
        return 1;
    long long best, run, x;
    if (scanf("%lld", &x) != 1)
        return 1;
    best = run = x;
    for (int i = 1; i < n; i++) {
        if (scanf("%lld", &x) != 1)
            return 1;
        run = (run > 0) ? run + x : x;
        if (run > best)
            best = run;
    }
    printf("%lld\n", best);
    return 0;
}
