"""Input generator: generate(size, rng) -> one test input.

`size` bounds the magnitude of the two numbers. The complexity analyzer uses
sizes 1, 10, 100 (the work should not grow); the edge-case runner also uses
sizes up to 2^31-1, where roughly a quarter of random pairs overflow an int.
"""

INT_MAX = 2**31 - 1


def generate(size, rng):
    bound = min(size, INT_MAX)
    return f"{rng.randint(-bound, bound)} {rng.randint(-bound, bound)}\n"
