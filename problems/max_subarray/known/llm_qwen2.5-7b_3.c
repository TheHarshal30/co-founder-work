#include <stdio.h>
#include <limits.h>

int maxSubarraySum(int arr[], int n) {
    int max_so_far = INT_MIN, max_ending_here = 0;
    
    // Iterate through the array
    for (int i = 0; i < n; i++) {
        // Add current element to max_ending_here
        max_ending_here += arr[i];
        
        // Update max_so_far if needed
        if (max_so_far < max_ending_here) {
            max_so_far = max_ending_here;
        }
        
        // If max_ending_here becomes negative, reset it to 0
        if (max_ending_here < 0) {
            max_ending_here = 0;
        }
    }
    
    return max_so_far;
}

int main() {
    int n;
    scanf("%d", &n);
    
    // Read the array elements
    int arr[n];
    for (int i = 0; i < n; i++) {
        scanf("%d", &arr[i]);
    }
    
    // Find and print the maximum subarray sum
    printf("%d\n", maxSubarraySum(arr, n));
    
    return 0;
}
