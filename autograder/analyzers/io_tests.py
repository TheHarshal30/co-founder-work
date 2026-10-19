"""correctness: run the hidden test set and compare outputs.

Each failing test becomes a counterexample: the input, the expected output, what
the program printed, and why. Failing inputs are re-run under ASan/UBSan so the
'why' can name the exact undefined behaviour (e.g. signed overflow at add.c:5).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from ..compare import MODES, shorten, verdict
from ..diagnostics import parse_sanitizer
from ..explain import GENERIC_FIX, explain, llm_explain
from .base import Analyzer, AnalyzerResult, Counterexample, register


def collect_tests(folder: Path) -> List[Path]:
    return sorted(folder.glob("*.in"))


def generated_cases(ctx, params) -> List[Tuple[str, str]]:
    """(name, input) for hidden tests produced by the problem's generator, e.g.
    {"name": "09_max_random", "size": 200000, "seed": 9}. Large tests need not be
    stored in the repository; the seed makes them identical on every run."""
    from ..context import seeded_rng

    out = []
    specs = params.get("generated", [])
    if specs:
        gen = ctx.generator(params.get("generator", "gen.py"))
        for spec in specs:
            out.append((str(spec["name"]), gen(int(spec["size"]),
                                               seeded_rng(spec.get("seed", 0), "hidden", spec["name"]))))
    return out


def diagnose(ctx, stdin: str) -> str:
    """First sanitizer finding for this input, or ''."""
    r = ctx.run_sanitized(stdin)
    if r is None:
        return ""
    issues = parse_sanitizer(r.stderr, ctx.name)
    return issues[0].short() if issues else ""


def make_counterexample(ctx, params, label, stdin, expected, run, v) -> Counterexample:
    """Failing run -> counterexample with input, expected, got, why and fix."""
    diag = diagnose(ctx, stdin) if params.get("diagnose", True) else ""
    why, fix = explain(v, expected, run.stdout, run, diag, ctx.rules.time_limit_s, len(stdin))
    ce = Counterexample(
        label=label, input=shorten(stdin), expected=shorten(expected),
        got=shorten(run.stdout) if run.stdout and v != "timeout" else f"({run.describe()})",
        verdict=v, why=why, fix=fix, diagnostic=diag or None, suggestion=fix,
    )
    if params.get("explain") == "llm":
        better = llm_explain(ctx, ce, params)
        if better:
            ce.why, ce.fix = better
            if fix == GENERIC_FIX:
                ce.suggestion = ce.fix
    return ce


@register
class IOTests(Analyzer):
    name = "io_tests"
    description = "Hidden input/output tests"
    needs_execution = True

    def validate(self, params, rules):
        problems = []
        d = params.get("testset_dir")
        if not d:
            return ["'testset_dir' is required"]
        folder = rules.resolve(d)
        if not folder.is_dir():
            problems.append(f"testset_dir not found: {d}")
        elif not collect_tests(folder) and not params.get("generated"):
            problems.append(f"no *.in files in {d}")
        for i, spec in enumerate(params.get("generated", [])):
            if not isinstance(spec, dict) or "name" not in spec or not isinstance(spec.get("size"), int):
                problems.append(f"generated[{i}] needs 'name' and integer 'size'")
        if params.get("generated"):
            if rules.reference is None:
                problems.append("'generated' tests need a 'reference' solution for expected outputs")
            if not rules.resolve(params.get("generator", "gen.py")).exists():
                problems.append("'generated' tests need the problem's generator (gen.py)")
        elif rules.reference is None and any(not p.with_suffix(".out").exists() for p in collect_tests(folder)):
            problems.append("some tests have no .out file and no 'reference' solution is configured")
        mode = params.get("compare", "trim")
        if mode not in MODES and not str(mode).startswith("float"):
            problems.append(f"'compare' must be one of {MODES} or float[:eps]")
        return problems

    def run(self, ctx, params):
        folder = ctx.rules.resolve(params["testset_dir"])
        mode = params.get("compare", "trim")
        max_shown = int(params.get("max_shown", 3))
        build = ctx.main_build()
        cases = []
        for case in collect_tests(folder):
            out_file = case.with_suffix(".out")
            cases.append((case.stem, case.read_text(), out_file.read_text() if out_file.exists() else None))
        cases += [(name, stdin, None) for name, stdin in generated_cases(ctx, params)]
        cases.sort(key=lambda c: c[0])

        results, failures = [], []
        for name, stdin, expected in cases:
            if expected is None:
                expected = ctx.expected_output(stdin)
            run = ctx.run(build, stdin)
            passed, v = verdict(run, expected, mode, params.get("require_zero_exit", True))
            results.append({"test": name, "verdict": v})
            if not passed:
                failures.append((name, stdin, expected, run, v))

        total, passed_n = len(results), sum(r["verdict"] == "pass" for r in results)
        counterexamples = [
            make_counterexample(ctx, params, f"test {name}", stdin, expected, run, v)
            for name, stdin, expected, run, v in failures[:max_shown]
        ]

        if total == passed_n:
            feedback = f"All {total} tests pass."
        else:
            first = counterexamples[0]
            feedback = (f"{passed_n}/{total} tests pass. First failure: {first.label} "
                        f"({first.verdict.replace('_', ' ')}).")
        return AnalyzerResult(
            score=passed_n / total if total else 0.0,
            feedback=feedback,
            evidence={"tests_passed": passed_n, "tests_total": total, "results": results,
                      "failing": [c.as_dict() for c in counterexamples]},
            counterexamples=counterexamples,
            suggestions=[c.suggestion for c in counterexamples if c.suggestion],
        )
