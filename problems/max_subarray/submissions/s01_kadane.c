/*
 * Maximum subarray sum (Kadane's algorithm), O(n) time, O(1) memory.
 * Values are read one at a time, so no array is needed.
 */
#include <stdio.h>

/* Best sum of a subarray ending at the current element, given the previous one. */
static long long extend_run(long long run_sum, long long value)
{
    return run_sum > 0 ? run_sum + value : value;
}

/* Read `count` values from stdin and return the maximum subarray sum. */
static long long max_subarray_sum(int count)
{
    long long value;
    if (scanf("%lld", &value) != 1)
        return 0;

    long long best = value;      /* best sum seen so far (non-empty subarray) */
    long long run_sum = value;   /* best sum of a subarray ending here */
    for (int i = 1; i < count; i++) {
        if (scanf("%lld", &value) != 1)
            break;
        run_sum = extend_run(run_sum, value);
        if (run_sum > best)
            best = run_sum;
    }
    return best;
}

int main(void)
{
    int count;
    if (scanf("%d", &count) != 1 || count < 1)
        return 1;
    printf("%lld\n", max_subarray_sum(count));
    return 0;
}
