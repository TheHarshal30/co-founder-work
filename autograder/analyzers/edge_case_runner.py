"""logical: hunt for inputs that break the program.

Two sources of probes, both compared against the instructor's reference
solution (so nobody has to hand-write expected outputs):

1. Named corner-case families from the problem's ``edge_cases.json``
   (e.g. "negatives", "zero", "int_overflow", "all_negative").
2. Random stress tests from the problem's generator, smallest sizes first and
   with a fixed seed. Because small inputs are tried first, the first failure
   found is already a short, readable counterexample -- and the same seed
   always finds the same one.

Score = mean over families of the fraction of probes passed (random tests count
as one extra family), so one broken corner case costs the same whether the
instructor wrote two probes for it or five.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Dict, List

from ..compare import MODES, verdict
from .base import Analyzer, AnalyzerResult, register
from .io_tests import make_counterexample

DEFAULT_SIZES = [1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50]


def load_cases(path) -> Dict[str, List[dict]]:
    raw = json.loads(path.read_text())
    out: Dict[str, List[dict]] = OrderedDict()
    for name, entries in raw.items():
        if name.startswith("_"):
            continue
        items = []
        for e in entries:
            items.append(e if isinstance(e, dict) else {"input": e})
        out[name] = items
    return out


@register
class EdgeCaseRunner(Analyzer):
    name = "edge_case_runner"
    description = "Corner cases + seeded random stress tests vs. the reference solution"
    needs_execution = True

    def validate(self, params, rules):
        problems = []
        cases = params.get("cases", "all")
        cases_file = rules.resolve(params.get("cases_file", "edge_cases.json"))
        if not cases_file.exists():
            problems.append(f"cases_file not found: {cases_file.name}")
        else:
            try:
                known = load_cases(cases_file)
            except (ValueError, AttributeError) as e:
                return [f"cases_file is not valid: {e}"]
            if cases != "all":
                if not isinstance(cases, list):
                    problems.append("'cases' must be a list of case-family names or \"all\"")
                else:
                    missing = [c for c in cases if c not in known]
                    if missing:
                        problems.append(f"unknown case families {missing}; "
                                        f"{cases_file.name} defines {list(known)}")
        if rules.reference is None and not _all_have_expected(cases_file):
            problems.append("needs a 'reference' solution to compute expected outputs")
        if int(params.get("random_tests", 0)) > 0:
            gen = rules.resolve(params.get("generator", "gen.py"))
            if not gen.exists():
                problems.append(f"random_tests > 0 but generator not found: {gen.name}")
        mode = params.get("compare", "trim")
        if mode not in MODES and not str(mode).startswith("float"):
            problems.append(f"'compare' must be one of {MODES} or float[:eps]")
        return problems

    def run(self, ctx, params):
        mode = params.get("compare", "trim")
        max_shown = int(params.get("max_shown", 3))
        all_cases = load_cases(ctx.rules.resolve(params.get("cases_file", "edge_cases.json")))
        wanted = params.get("cases", "all")
        families = OrderedDict(
            (k, v) for k, v in all_cases.items() if wanted == "all" or k in wanted
        )
        if wanted != "all":  # keep the instructor's order
            families = OrderedDict((k, families[k]) for k in wanted)

        n_random = int(params.get("random_tests", 0))
        if n_random > 0:
            gen = ctx.generator(params.get("generator", "gen.py"))
            sizes = params.get("sizes", DEFAULT_SIZES)
            seed = params.get("seed", 42)
            from ..context import seeded_rng

            probes = []
            for i in range(n_random):
                size = sizes[i * len(sizes) // n_random]
                probes.append({"input": gen(size, seeded_rng(seed, "edge", size, i)),
                               "note": f"random, size {size}, seed {seed}#{i}"})
            families["random_stress"] = probes

        build = ctx.main_build()
        per_family = []
        failures = []
        for fam, probes in families.items():
            passed = 0
            for probe in probes:
                stdin = probe["input"]
                expected = probe.get("expected")
                if expected is None:
                    expected = ctx.expected_output(stdin)
                run = ctx.run(build, stdin)
                ok, v = verdict(run, expected, mode, params.get("require_zero_exit", True))
                if ok:
                    passed += 1
                else:
                    failures.append((fam, probe, stdin, expected, run, v))
            per_family.append({"family": fam, "passed": passed, "total": len(probes)})

        # One counterexample per failing family first, so the student sees the
        # variety of problems rather than three variations of the same one.
        shown, seen_fams = [], set()
        for f in failures:
            if f[0] not in seen_fams:
                shown.append(f)
                seen_fams.add(f[0])
        shown = shown[:max_shown]

        counterexamples = [
            make_counterexample(
                ctx, params, f"edge case '{fam}'" + (f" ({probe['note']})" if probe.get("note") else ""),
                stdin, expected, run, v)
            for fam, probe, stdin, expected, run, v in shown
        ]

        score = sum(f["passed"] / f["total"] for f in per_family if f["total"]) / max(1, len(per_family))
        failing_fams = [f["family"] for f in per_family if f["passed"] < f["total"]]
        if not failing_fams:
            feedback = f"Handles every corner-case family tested ({', '.join(families)})."
        else:
            feedback = f"Fails on: {', '.join(failing_fams)}."
        return AnalyzerResult(
            score=score, feedback=feedback,
            evidence={"families": per_family, "failing": [c.as_dict() for c in counterexamples]},
            counterexamples=counterexamples,
            suggestions=[c.suggestion for c in counterexamples if c.suggestion],
        )


def _all_have_expected(cases_file) -> bool:
    try:
        return all("expected" in p for ps in load_cases(cases_file).values() for p in ps)
    except Exception:
        return False
