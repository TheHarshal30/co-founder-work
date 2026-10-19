#include <stdio.h>
#include <limits.h>

int main() {
    int n;
    scanf("%d", &n);
    
    long long max_ending_here = 0, max_so_far = LLONG_MIN;
    for (int i = 0; i < n; i++) {
        int a_i;
        scanf("%d", &a_i);
        max_ending_here += a_i;
        if (max_ending_here > max_so_far) {
            max_so_far = max_ending_here;
        }
        if (max_ending_here < 0) {
            max_ending_here = 0;
        }
    }

    printf("%lld\n", max_so_far);
    return 0;
}
