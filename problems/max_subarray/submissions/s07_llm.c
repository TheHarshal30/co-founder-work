#include <stdio.h>
#include <limits.h>

int maxSum(int nums[], int len) {
    int best = INT_MIN, here = 0;

    // Iterate through the array
    for (int i = 0; i < len; i++) {
        // Add current element to here
        here += nums[i];

        // Update best if needed
        if (best < here) {
            best = here;
        }

        // If here becomes negative, reset it to 0
        if (here < 0) {
            here = 0;
        }
    }

    return best;
}

int main() {
    int len;
    scanf("%d", &len);

    // Read the array elements
    int nums[len];
    for (int i = 0; i < len; i++) {
        scanf("%d", &nums[i]);
    }

    // Find and print the maximum subarray sum
    printf("%d\n", maxSum(nums, len));

    return 0;
}
