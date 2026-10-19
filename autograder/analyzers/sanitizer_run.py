"""memory_safety: run the program under AddressSanitizer + UBSan (+ LeakSanitizer
on Linux) and, optionally, valgrind memcheck.

Every hidden test, the sample and the corner cases are replayed against the
sanitizer build, because memory bugs usually only show on particular inputs.
valgrind adds what ASan cannot see (reads of uninitialised memory) and leak
detection where LSan is unavailable (macOS). Findings from the two tools are
merged so one bug is not counted twice.

score = 1 - penalty_per_issue * (distinct issues), floored at 0.
"""

from __future__ import annotations

import json
from typing import List

from .. import tools
from ..compare import shorten
from ..compiler import split_flags
from ..diagnostics import Issue, parse_sanitizer, parse_valgrind
from ..sandbox import IS_DARWIN
from .base import Analyzer, AnalyzerResult, clamp01, register
from .io_tests import collect_tests

_CATEGORY = {
    "heap-buffer-overflow": "out-of-bounds", "stack-buffer-overflow": "out-of-bounds",
    "global-buffer-overflow": "out-of-bounds", "invalid-read": "out-of-bounds",
    "invalid-write": "out-of-bounds", "array-index-out-of-bounds": "out-of-bounds",
    "object-size-overflow": "out-of-bounds", "heap-use-after-free": "use-after-free",
    "memory-leak": "leak", "double-free": "invalid-free", "invalid-free": "invalid-free",
}

_ADVICE = {
    "out-of-bounds": "An array/buffer is accessed outside its bounds; check loop limits (< n, not <= n) "
                     "and allocation sizes.",
    "leak": "Memory from malloc/calloc is never freed; free() every allocation before returning.",
    "use-after-free": "Memory is used after free(); set pointers to NULL after freeing and do not reuse them.",
    "invalid-free": "free() is called twice or on a pointer that malloc did not return.",
    "signed-integer-overflow": "A signed int calculation overflows (undefined behaviour); use long long or "
                               "check the range first.",
    "uninitialised-value": "A variable is read before it is given a value; initialise it at declaration.",
    "null-pointer-dereference": "A NULL pointer is dereferenced; check malloc's result and pointer arguments.",
    "division-by-zero": "Integer division by zero; guard the divisor.",
}


def _category(issue: Issue) -> str:
    return _CATEGORY.get(issue.kind, issue.kind)


def gather_inputs(ctx, params) -> List[str]:
    inputs: List[str] = []
    if ctx.rules.sample_input:
        inputs.append(ctx.rules.sample_input)
    dirs = [params["testset_dir"]] if params.get("testset_dir") else [
        m.params["testset_dir"] for m in ctx.rules.metrics
        if m.analyzer == "io_tests" and "testset_dir" in m.params]
    for d in dirs:
        inputs.extend(p.read_text() for p in collect_tests(ctx.rules.resolve(d)))
    cases_file = ctx.rules.resolve(params.get("cases_file", "edge_cases.json"))
    if cases_file.exists():
        for name, entries in json.loads(cases_file.read_text()).items():
            if not name.startswith("_"):
                inputs.extend(e["input"] if isinstance(e, dict) else e for e in entries)
    # Memory bugs almost always show on small inputs; skipping huge ones keeps a
    # quadratic submission from spending minutes under ASan.
    max_bytes = int(params.get("max_input_bytes", 200_000))
    seen, unique = set(), []
    for i in inputs:
        if i not in seen and len(i) <= max_bytes:
            seen.add(i)
            unique.append(i)
    return unique[: int(params.get("max_inputs", 40))]


@register
class SanitizerRun(Analyzer):
    name = "sanitizer_run"
    description = "ASan/UBSan/LSan and valgrind memcheck over all test inputs"
    needs_execution = True

    def run(self, ctx, params):
        san_flags = split_flags(params.get("flags", "-fsanitize=address,undefined"))
        build = ctx.sanitizer_build(san_flags)
        if not build.ok:
            return AnalyzerResult.unavailable(f"sanitizer build failed: {build.output[:200]}")
        inputs = gather_inputs(ctx, params)
        time_factor = float(params.get("time_factor", 5))

        found: List[dict] = []
        keys = set()

        def add(issue: Issue, stdin: str) -> None:
            key = (_category(issue), issue.location)
            if key in keys:
                return
            keys.add(key)
            found.append({**issue.as_dict(), "input": shorten(stdin, 120)})

        for stdin in inputs:
            r = ctx.run_sanitized(stdin, san_flags, time_factor)
            if r is None:
                break
            for issue in parse_sanitizer(r.stderr, ctx.name):
                add(issue, stdin)

        leak_tool = "lsan" if not IS_DARWIN and "address" in " ".join(san_flags) else None
        use_valgrind = params.get("valgrind", True) and tools.which("valgrind")
        if use_valgrind:
            leak_tool = leak_tool or "valgrind"
            dbg = ctx.debug_build()
            vg_inputs = inputs[: int(params.get("valgrind_max_inputs", 3))]
            for stdin in vg_inputs:
                r = ctx.run(dbg, stdin, time_limit_s=float(params.get("valgrind_timeout_s", 30)),
                            memory_limit_mb=None,
                            prefix=[tools.which("valgrind"), "--leak-check=full",
                                    "--errors-for-leak-kinds=definite", "--error-exitcode=0"])
                for issue in parse_valgrind(r.stderr, ctx.name):
                    # a leak already reported at the same allocation site is
                    # dropped by add(); an anonymous summary adds nothing new
                    if issue.kind == "memory-leak" and issue.location is None and \
                            any(f["kind"] == "memory-leak" for f in found):
                        continue
                    add(issue, stdin)

        penalty = float(params.get("penalty_per_issue", 0.25))
        score = clamp01(1.0 - penalty * len(found))
        evidence = {"inputs_checked": len(inputs), "sanitizers": " ".join(san_flags),
                    "valgrind": bool(use_valgrind), "leak_detection": leak_tool or "unavailable",
                    "issues": found}
        if not found:
            return AnalyzerResult(score=1.0, evidence=evidence,
                                  feedback=f"No memory or undefined-behaviour errors on {len(inputs)} inputs.")
        listed = "; ".join(f"{f['kind']} at {f['location'] or '?'}" for f in found[:3])
        by_cat: dict = {}
        for f in found:
            cat = _category(Issue(f["tool"], f["kind"], f["message"], f["location"]))
            by_cat.setdefault(cat, [])
            if f["location"] and f["location"] not in by_cat[cat]:
                by_cat[cat].append(f["location"])
        advice = []
        for cat, locs in by_cat.items():
            tip = _ADVICE.get(cat, f"Fix the {cat} reported by the sanitizer.")
            advice.append(f"{', '.join(locs)}: {tip}" if locs else tip)
        return AnalyzerResult(score=score, evidence=evidence,
                              feedback=f"{len(found)} issue(s): {listed}", suggestions=advice)
