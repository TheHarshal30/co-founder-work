# max_subarray

The first line contains n (1 <= n <= 200000). The second line contains n
integers a_1 .. a_n, each with |a_i| <= 10^9.

Print the largest possible sum of a non-empty contiguous subarray, followed by
a newline.

Notes: the subarray must contain at least one element, so if every number is
negative the answer is the largest (least negative) number. The answer can be
as large as 2 * 10^14, which does not fit in a 32-bit int.

Required time complexity: O(n). Time limit: 1 second.

Sample input:

    9
    -2 1 -3 4 -1 2 1 -5 4

Sample output:

    6
