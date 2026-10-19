#include <stdio.h>
static long long plus(long long p, long long q) { return p + q; }
int main(void) {
  long long u = 0; long long v = 0;
  if (scanf("%lld %lld", &u, &v) != 2) { fprintf(stderr, "bad input\n"); return 1; }
  printf("%lld\n", plus(u, v));
  return 0;
}
