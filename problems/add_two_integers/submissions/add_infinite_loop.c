#include <stdio.h>

/* Sum every number on the input. */
int main(void)
{
    int value, total = 0;
    while (scanf("%d", &value) != 0)   /* scanf returns EOF (-1) at end of input, not 0 */
        total += value;
    printf("%d\n", total);
    return 0;
}
