#include <stdio.h>
#include <stdlib.h>

/* Try every subarray [i, j] and keep the best sum. */
long long best_sum(const long long *a, int n)
{
    long long best = a[0];
    for (int i = 0; i < n; i++) {
        long long sum = 0;
        for (int j = i; j < n; j++) {
            sum += a[j];
            if (sum > best)
                best = sum;
        }
    }
    return best;
}

int main(void)
{
    int n;
    if (scanf("%d", &n) != 1)
        return 1;
    long long *a = malloc(n * sizeof *a);
    for (int i = 0; i < n; i++)
        scanf("%lld", &a[i]);
    printf("%lld\n", best_sum(a, n));
    free(a);
    return 0;
}
