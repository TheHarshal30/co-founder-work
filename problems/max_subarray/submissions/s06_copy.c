#include <stdio.h>
static long long step(long long acc, long long v) { return acc > 0 ? acc + v : v; }
static long long solve(int m) {
  long long v;
  if (scanf("%lld", &v) != 1) return 0;
  long long top = v; long long acc = v;
  for (int k = 1; k < m; k++) {
    if (scanf("%lld", &v) != 1) break;
    acc = step(acc, v);
    if (acc > top) top = acc;
  }
  return top;
}
int main(void) {
  int m;
  if (scanf("%d", &m) != 1 || m < 1) return 1;
  printf("%lld\n", solve(m));
  return 0;
}
