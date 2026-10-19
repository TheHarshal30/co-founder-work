#include <stdio.h>
#include <limits.h>

int main() {
    int n;
    scanf("%d", &n);

    long long max_so_far = LLONG_MIN;  // Initialize to the smallest possible value
    long long current_max = 0;

    for (int i = 0; i < n; i++) {
        int a_i;
        scanf("%d", &a_i);
        
        // Update current_max considering the current element and previous max subarray sum
        current_max = current_max + a_i > a_i ? current_max + a_i : a_i;

        // Update max_so_far if current_max is greater than max_so_far
        if (current_max > max_so_far) {
            max_so_far = current_max;
        }
    }

    printf("%lld\n", max_so_far);

    return 0;
}
