"""Input generator: generate(size, rng) -> one test input with n = size.

Small sizes use small values so a failing random case is easy to read in a
report; large sizes use the full |a_i| <= 10^9 range.
"""


def generate(size, rng):
    n = max(1, size)
    bound = 20 if n <= 12 else 10**9
    values = [rng.randint(-bound, bound) for _ in range(n)]
    return f"{n}\n{' '.join(map(str, values))}\n"
