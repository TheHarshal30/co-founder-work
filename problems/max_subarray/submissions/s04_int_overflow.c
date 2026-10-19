#include <stdio.h>

int max(int a, int b) { return a > b ? a : b; }

int main(void)
{
    int n, x;
    scanf("%d", &n);
    scanf("%d", &x);
    int best = x, cur = x;
    for (int i = 1; i < n; i++) {
        scanf("%d", &x);
        cur = max(x, cur + x);
        best = max(best, cur);
    }
    printf("%d\n", best);
    return 0;
}
