#include <stdio.h>
#include <stdlib.h>

int main(void)
{
    int *nums = malloc(2 * sizeof(int));
    long long total = 0;
    scanf("%d %d", &nums[0], &nums[1]);
    for (int i = 0; i <= 2; i++)   /* off-by-one: also reads nums[2] */
        total += nums[i];
    printf("%lld\n", total);
    return 0;                      /* nums is never freed */
}
