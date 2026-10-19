/*
 * add_clean.c - read two integers and print their exact sum.
 * The sum is computed in long long so that INT_MAX + 1 does not overflow.
 */
#include <stdio.h>

/* Return lhs + rhs; 64-bit arithmetic cannot overflow for 32-bit inputs. */
static long long add(long long lhs, long long rhs)
{
    return lhs + rhs;
}

int main(void)
{
    long long first = 0;
    long long second = 0;

    /* Reject malformed input instead of printing a garbage sum. */
    if (scanf("%lld %lld", &first, &second) != 2) {
        fprintf(stderr, "expected two integers\n");
        return 1;
    }

    printf("%lld\n", add(first, second));
    return 0;
}
