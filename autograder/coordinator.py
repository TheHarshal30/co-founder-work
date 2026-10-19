"""The main agent: first pass -> plan -> dispatch analyzers -> collate.

    submission.c + rules.json
            |
            v
    1. first pass   compile, smoke run on the sample input, static sanity
            |        (does not compile / hangs -> skip execution analyzers,
            |         still run source-only ones, emit a partial report)
            v
    2. dispatch     each metric's analyzer, in parallel, with its params
            v
    3. collate      grade = sum(score * weight); counterexamples, flags,
                    suggestions ordered by points lost

The policy is deterministic on purpose: the LLM is used as a *tool* for
subjective judgements (with cached answers), never to decide what to run --
otherwise the same submission could take a different path and get a
different grade.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import sandbox, tools
from .analyzers import REGISTRY
from .analyzers.base import AnalyzerResult
from .context import Context, ReferenceError
from .ctokens import strip_comments
from .llm import LLMClient
from .rules import Rules, hash_paths

REPORT_VERSION = 1

_READS = re.compile(r"\b(scanf|fscanf|getchar|fgets|gets|getline|getc|fgetc|fread|read)\s*\(")
_WRITES = re.compile(r"\b(printf|puts|putchar|fputs|fprintf|fwrite|write|putc|fputc)\s*\(")
_SANITY_MESSAGES = {
    "has_main": "no main() function found",
    "reads_input": "no input call (scanf, fgets, getchar, ...) found",
    "writes_output": "no output call (printf, puts, putchar, ...) found",
}


@dataclass
class FirstPass:
    compiled: bool
    compile_warnings: int
    smoke_ok: bool
    smoke_status: str
    suspected_infinite_loop: bool
    static_sanity: Dict[str, bool]
    notes: List[str] = field(default_factory=list)
    compiler_output: str = ""


def first_pass(ctx: Context) -> FirstPass:
    code = strip_comments(ctx.source)
    sanity = {
        "has_main": bool(re.search(r"\bmain\s*\(", code)),
        "reads_input": bool(_READS.search(code)),
        "writes_output": bool(_WRITES.search(code)),
        "has_return": bool(re.search(r"\breturn\b", code)),
    }
    notes = [f"static sanity: {_SANITY_MESSAGES[k]}" for k, v in sanity.items()
             if not v and k in _SANITY_MESSAGES]
    build = ctx.main_build()
    if not build.ok:
        first_error = build.errors[0] if build.errors else None
        notes.append("compile failed" + (f": line {first_error.line}: {first_error.message}" if first_error else ""))
        return FirstPass(False, len(build.warnings), False, "not_run", False, sanity, notes, build.output[-2000:])
    run = ctx.run(build, ctx.rules.sample_input or "")
    hang = run.status == "timeout"
    if hang:
        notes.append(f"smoke run on the sample input did not halt within {ctx.rules.time_limit_s:g}s")
    elif not run.ok:
        notes.append(f"smoke run: program {run.describe()}")
    return FirstPass(True, len(build.warnings), run.ok, run.status, hang, sanity, notes, build.output[-2000:])


def _skip(reason: str) -> AnalyzerResult:
    return AnalyzerResult(score=0.0, feedback=f"Skipped: {reason}", status="skipped")


def _run_one(ctx: Context, metric) -> AnalyzerResult:
    analyzer = REGISTRY[metric.analyzer]()
    try:
        return analyzer.run(ctx, metric.params)
    except ReferenceError as e:
        return AnalyzerResult(score=None, feedback=f"Not measured: {e}", status="error")
    except Exception as e:  # an analyzer bug must not take the whole report down
        return AnalyzerResult(score=None, feedback=f"Analyzer error: {type(e).__name__}: {e}",
                              status="error")


def grade(
    submission: Path,
    rules: Rules,
    llm: Optional[LLMClient] = None,
    max_workers: Optional[int] = None,
    keep_workdir: bool = False,
) -> Dict[str, Any]:
    started = time.monotonic()
    llm = llm or LLMClient()
    stats_before = dict(llm.stats)
    workdir = Path(tempfile.mkdtemp(prefix="autograder-"))
    timings: Dict[str, float] = {}
    try:
        ctx = Context(Path(submission), rules, llm, workdir)
        t0 = time.monotonic()
        fp = first_pass(ctx)
        ctx.first_pass = fp
        timings["first_pass"] = round(time.monotonic() - t0, 3)

        results: Dict[str, AnalyzerResult] = {}
        to_run = []
        for m in rules.metrics:
            needs_exec = REGISTRY[m.analyzer].needs_execution
            if needs_exec and not fp.compiled:
                results[m.name] = _skip("the program does not compile")
            elif needs_exec and fp.suspected_infinite_loop:
                results[m.name] = _skip("the program did not halt on the sample input (suspected infinite loop)")
            else:
                to_run.append(m)

        def timed(m):
            t = time.monotonic()
            r = _run_one(ctx, m)
            timings[m.name] = round(time.monotonic() - t, 3)
            return m.name, r

        with ThreadPoolExecutor(max_workers=max_workers or min(8, max(1, len(to_run)))) as pool:
            for name, r in pool.map(timed, to_run):
                results[name] = r

        report = collate(ctx, fp, results)
        report["meta"] = {
            "report_version": REPORT_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_s": round(time.monotonic() - started, 2),
            "timings_s": timings,
            "llm_cache": {k: llm.stats[k] - stats_before.get(k, 0) for k in llm.stats},
            "memory_limit_enforced": sandbox.memory_limit_enforced(),
        }
        return report
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def collate(ctx: Context, fp: FirstPass, results: Dict[str, AnalyzerResult]) -> Dict[str, Any]:
    rules = ctx.rules
    metrics_out, flags, suggestions = [], [], []
    measured_weight = 0.0
    total = 0.0
    for m in rules.metrics:
        r = results[m.name]
        entry: Dict[str, Any] = {"name": m.name, "analyzer": m.analyzer, "weight": m.weight,
                                 "status": r.status}
        if r.score is None:
            entry.update({"score": None, "contribution": None})
        else:
            score = round(r.score, 4)
            contribution = round(score * m.weight * 100, 2)
            measured_weight += m.weight
            total += score * m.weight * 100
            entry.update({"score": score, "contribution": contribution})
            lost = round((1 - score) * m.weight * 100, 2)
            for s in r.suggestions:
                suggestions.append({"metric": m.name, "points_lost": lost, "suggestion": s})
        entry["feedback"] = r.feedback
        entry["evidence"] = r.evidence
        if r.counterexamples:
            entry["counterexamples"] = [c.as_dict() for c in r.counterexamples]
        metrics_out.append(entry)
        for f in r.flags:
            flags.append({"metric": m.name, **f})
        if m.analyzer == "complexity_estimator" and r.evidence.get("deterministic") is False:
            flags.append({"metric": m.name, "type": "nondeterministic_measurement",
                          "detail": "valgrind unavailable; complexity measured with CPU time, which "
                                    "varies between runs. Grade in the Docker image for a reproducible result."})

    complete = abs(measured_weight - sum(m.weight for m in rules.metrics)) < 1e-9
    if complete:
        grade_value = round(total, 1)
    else:
        grade_value = round(total / measured_weight, 1) if measured_weight else 0.0
        missing = [e["name"] for e in metrics_out if e["score"] is None]
        flags.append({"type": "incomplete_grade",
                      "detail": f"Not measured: {', '.join(missing)}. Grade is scaled over the remaining "
                                f"weight; re-run when the missing tool is available before publishing."})
    if fp.suspected_infinite_loop:
        flags.insert(0, {"type": "suspected_infinite_loop",
                         "detail": "No output on the sample input within the time limit; execution-based "
                                   "metrics were skipped (partial report)."})
    if not fp.compiled:
        flags.insert(0, {"type": "compile_failed", "detail": "; ".join(fp.notes)})

    # Biggest wins first: suggestions from the metrics that lost the most points.
    seen, ordered = set(), []
    for s in sorted(suggestions, key=lambda s: -s["points_lost"]):
        if s["suggestion"] not in seen and s["points_lost"] > 0:
            seen.add(s["suggestion"])
            ordered.append(s)

    # The same input often fails in several metrics (a hidden test and an edge
    # case); the student sees it once, under the first metric that found it.
    # Inputs that fail for the same reason are folded into one entry
    # ("same problem on: test 10, ...") so the student sees each mistake once.
    counterexamples, seen_inputs, by_reason = [], set(), {}
    for e in metrics_out:
        for c in e.get("counterexamples", []):
            if c["input"] in seen_inputs:
                continue
            seen_inputs.add(c["input"])
            reason = (c["verdict"], c["fix"])
            if reason in by_reason:
                by_reason[reason].setdefault("same_problem_on", []).append(c["label"])
                continue
            entry = {"metric": e["name"], **c}
            by_reason[reason] = entry
            counterexamples.append(entry)

    report: Dict[str, Any] = {
        "submission": ctx.name,
        "submission_sha256": ctx.source_sha256,
        "problem": rules.problem,
        "rules_hash": "sha256:" + rules.sha256,
        "assets_hash": "sha256:" + hash_paths(ctx.asset_paths()),
        "grade": grade_value,
        "grade_complete": complete,
        "partial": fp.suspected_infinite_loop or not fp.compiled,
        "first_pass": {k: v for k, v in asdict(fp).items() if k != "compiler_output"},
        "metrics": metrics_out,
        "counterexamples": counterexamples,
        "flags": flags,
        "suggestions": ordered[:8],
        "tool_versions": tools.snapshot(rules.cc),
    }
    if rules.include_usefulness_limitations:
        report["usefulness_limitations"] = usefulness_limitations()
    report["fingerprint"] = fingerprint(report)
    return report


def fingerprint(report: Dict[str, Any]) -> str:
    """Hash of everything grade-relevant (all but timing metadata). Two runs with
    equal fingerprints produced byte-identical grades and feedback."""
    body = {k: v for k, v in report.items() if k not in ("meta", "fingerprint")}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def usefulness_limitations() -> Dict[str, Any]:
    path = Path(__file__).parent / "data" / "usefulness_limitations.json"
    return json.loads(path.read_text())
