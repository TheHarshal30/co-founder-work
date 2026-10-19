#include <stdio.h>
#include <stdlib.h>

int main()
{
    int n;
    scanf("%d", &n);
    long long *arr = (long long *)malloc(n * sizeof(long long));
    for (int i = 1; i <= n; i++)          /* 1-based loop writes arr[n] */
        scanf("%lld", &arr[i]);
    long long best = arr[1], cur = 0;
    for (int i = 1; i <= n; i++) {
        cur = cur > 0 ? cur + arr[i] : arr[i];
        if (cur > best) best = cur;
    }
    printf("%lld\n", best);
    return 0;                             /* arr is never freed */
}
