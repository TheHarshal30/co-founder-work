"""complexity: estimate the growth rate empirically and compare it to the bound.

Method
------
1. The problem's generator makes one input per requested size (fixed seed).
2. Each input is run under valgrind's callgrind, which counts *instructions
   executed*. Unlike stopwatch time, an instruction count is the same on every
   run -- so the estimate, and the grade, are reproducible. (Without valgrind we
   fall back to median CPU time, and the report says the metric is noisy.)
3. We fit cost(n) = a + b*f(n) for f in 1, log n, n, n log n, n^2, n^3, 2^n by
   least squares and keep the best fit, preferring the simpler class when two
   fit almost equally well. The intercept a absorbs fixed start-up cost (libc,
   reading the first line), which otherwise hides the true growth at small n.
4. Score: 1.0 if the estimate is within the required bound, minus
   ``penalty_per_class`` for every class above it.

The same callgrind run gives a profile: the source line that executes the most
instructions at the largest size is reported as the hot spot.
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .. import tools
from ..context import seeded_rng
from .base import Analyzer, AnalyzerResult, clamp01, register

CLASSES: List[Tuple[str, object]] = [
    ("O(1)", lambda n: 0.0),
    ("O(log n)", lambda n: math.log2(n) if n > 1 else 0.0),
    ("O(n)", lambda n: float(n)),
    ("O(n log n)", lambda n: n * math.log2(n) if n > 1 else 0.0),
    ("O(n^2)", lambda n: float(n) ** 2),
    ("O(n^3)", lambda n: float(n) ** 3),
    ("O(2^n)", lambda n: 2.0 ** n if n < 1000 else math.inf),
]
ORDER = [c for c, _ in CLASSES]

_ALIASES = {
    "o(1)": "O(1)", "o(logn)": "O(log n)", "o(lgn)": "O(log n)", "o(n)": "O(n)",
    "o(nlogn)": "O(n log n)", "o(nlgn)": "O(n log n)", "o(n^2)": "O(n^2)", "o(n2)": "O(n^2)",
    "o(n²)": "O(n^2)", "o(n*n)": "O(n^2)", "o(n^3)": "O(n^3)", "o(n3)": "O(n^3)",
    "o(n³)": "O(n^3)", "o(2^n)": "O(2^n)",
}


def normalize_class(text: str) -> Optional[str]:
    return _ALIASES.get(re.sub(r"\s+", "", str(text)).lower())


def _fit(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float, float]:
    """Least squares y = a + b x with b >= 0. Returns (a, b, normalised RMSE)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0
    if b < 0:
        b = 0.0
    a = my - b * mx
    rmse = math.sqrt(sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys)) / n)
    return a, b, rmse / my if my else 0.0


def estimate_class(sizes: Sequence[int], costs: Sequence[float],
                   flat_tolerance: float = 0.05, tie_tolerance: float = 0.01) -> Tuple[str, Dict[str, float]]:
    lo, hi = min(costs), max(costs)
    errors: Dict[str, float] = {}
    for name, f in CLASSES:
        xs = [f(n) for n in sizes]
        if any(math.isinf(x) or x > 1e150 for x in xs):
            continue  # its least-squares sums would overflow a float (2^n at n ~ 512..999 did)
        errors[name] = round(_fit(xs, costs)[2], 6)
    if lo > 0 and (hi - lo) / lo <= flat_tolerance:
        return "O(1)", errors
    best = min(errors.values())
    # simplest class whose fit is (almost) as good as the best one
    for name in ORDER:
        if name in errors and errors[name] <= best + tie_tolerance:
            return name, errors
    return min(errors, key=errors.get), errors  # pragma: no cover


def parse_callgrind(path: Path, source_name: str) -> Tuple[int, Dict[str, int], Dict[int, int]]:
    """(total instructions, self cost per user function, self cost per user line)."""
    total = 0
    per_fn: Dict[str, int] = defaultdict(int)
    per_line: Dict[int, int] = defaultdict(int)
    fl = fi = fn = ""
    skip_next = False
    for raw in path.read_text(errors="replace").splitlines():
        if raw.startswith(("summary:", "totals:")):
            total = max(total, int(raw.split()[1]))
            continue
        if raw.startswith("fl="):
            fl = fi = raw[3:]
        elif raw.startswith(("fi=", "fe=")):
            fi = raw[3:]
        elif raw.startswith("fn="):
            fn, fi = raw[3:], fl
        elif raw.startswith("calls="):
            skip_next = True  # the next cost line is the callee's inclusive cost
        elif raw[:1].isdigit():
            if skip_next:
                skip_next = False
                continue
            parts = raw.split()
            if len(parts) >= 2 and Path(fi).name == source_name:
                cost = int(parts[1])
                per_fn[fn] += cost
                per_line[int(parts[0])] += cost
    return total, dict(per_fn), dict(per_line)


@register
class ComplexityEstimator(Analyzer):
    name = "complexity_estimator"
    description = "Empirical growth rate (instruction counts) vs. the required bound"
    needs_execution = True

    def validate(self, params, rules):
        problems = []
        if normalize_class(params.get("required", "")) is None:
            problems.append(f"'required' must be one of {ORDER}")
        sizes = params.get("sizes")
        if not isinstance(sizes, list) or len(sizes) < 3 or not all(isinstance(s, int) and s > 0 for s in sizes):
            problems.append("'sizes' must be a list of at least 3 positive integers")
        gen = rules.resolve(params.get("generator", "gen.py"))
        if not gen.exists():
            problems.append(f"generator not found: {gen.name}")
        return problems

    def run(self, ctx, params):
        required = normalize_class(params["required"])
        sizes = sorted(params["sizes"])
        seed = params.get("seed", 42)
        timeout = float(params.get("timeout_s", 60))
        method = params.get("method", "instructions")
        if method == "instructions" and not tools.which("valgrind"):
            method = "cpu_time"
        gen = ctx.generator(params.get("generator", "gen.py"))
        build = ctx.build("profile", ctx.rules.compile_flags + ["-g"])
        if not build.ok:
            return AnalyzerResult(score=0.0, feedback="Could not build for profiling.", status="skipped")

        points, hotspot, failure = [], None, None
        for size in sizes:
            stdin = gen(size, seeded_rng(seed, "complexity", size))
            if method == "instructions":
                cost, profile, fail = self._instructions(ctx, build, stdin, timeout)
            else:
                cost, profile, fail = self._cpu_time(ctx, build, stdin, timeout, int(params.get("repeats", 5)))
            if fail:
                failure = (size, fail)
                break
            points.append({"n": size, "cost": cost})
            if profile:
                hotspot = profile

        evidence = {"required": required, "method": method,
                    "deterministic": method == "instructions", "measurements": points}
        if failure:
            size, why = failure
            evidence["failed_at_size"] = size
            return AnalyzerResult(
                score=0.0, evidence=evidence,
                feedback=f"Could not measure growth: the program {why} at n={size}.",
                suggestions=[f"Make sure the program handles an input of size {size} within the limits."],
            )

        estimate, errors = estimate_class([p["n"] for p in points], [p["cost"] for p in points],
                                          float(params.get("flat_tolerance", 0.05)))
        gap = ORDER.index(estimate) - ORDER.index(required)
        score = 1.0 if gap <= 0 else clamp01(1.0 - float(params.get("penalty_per_class", 0.5)) * gap)
        evidence.update({"estimated": estimate, "fit_error": errors})
        if hotspot and hotspot["share"] >= 0.05:  # below that, start-up code dominates
            evidence["hotspot"] = hotspot

        unit = "instructions" if method == "instructions" else "s CPU"
        series = ", ".join(f"n={p['n']}: {p['cost']:,}" if method == "instructions"
                           else f"n={p['n']}: {p['cost']:.4f}" for p in points)
        if gap <= 0:
            feedback = f"{estimate} confirmed (required {required})."
        else:
            feedback = f"Estimated {estimate}, required {required} ({series} {unit})."
        suggestions = []
        if gap > 0:
            hot = evidence.get("hotspot")
            hint = (f" Most of the work happens at line {hot['line']} in {hot['function']}() "
                    f"({hot['share']:.0%} of instructions at n={points[-1]['n']}).") if hot else ""
            suggestions.append(f"Your algorithm grows like {estimate} but the problem requires {required}; "
                               f"look for a nested loop or repeated work you can remove.{hint}")
        return AnalyzerResult(score=score, feedback=feedback, evidence=evidence, suggestions=suggestions)

    # -- measurement back ends ----------------------------------------------
    def _instructions(self, ctx, build, stdin, timeout):
        out = Path(ctx.scratch) / f"callgrind-{hashlib.sha256(stdin.encode()).hexdigest()[:16]}.out"
        run = ctx.run(
            build, stdin, time_limit_s=timeout, wall_limit_s=timeout * 1.5, memory_limit_mb=None,
            prefix=[tools.which("valgrind"), "--tool=callgrind", f"--callgrind-out-file={out}",
                    "--compress-strings=no", "--compress-pos=no", "--dump-instr=no"],
        )
        if run.status == "timeout":
            return None, None, f"did not finish within {timeout:g}s (under instrumentation)"
        if not out.exists():
            return None, None, run.describe()
        total, per_fn, per_line = parse_callgrind(out, ctx.name)
        out.unlink()
        if run.status not in ("ok", "nonzero_exit"):
            return None, None, run.describe()
        hotspot = None
        if per_line and total:
            line, cost = max(per_line.items(), key=lambda kv: (kv[1], -kv[0]))
            fn = max(per_fn.items(), key=lambda kv: (kv[1], kv[0]))[0] if per_fn else None
            hotspot = {"line": line, "function": fn, "share": round(cost / total, 3),
                       "user_code_share": round(sum(per_line.values()) / total, 3)}
        return total, hotspot, None

    def _cpu_time(self, ctx, build, stdin, timeout, repeats):
        times = []
        for _ in range(max(1, repeats)):
            run = ctx.run(build, stdin, time_limit_s=timeout)
            if run.status not in ("ok", "nonzero_exit"):
                return None, None, run.describe()
            times.append(run.cpu_time_s)
        return round(statistics.median(times), 4), None, None
