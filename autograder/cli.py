"""Command line: grade one file, grade a folder, verify reproducibility, check rules.

    autograder grade  SUBMISSION.c --rules RULES.json [--out reports/]
    autograder batch  FOLDER       --rules RULES.json [--out reports/]
    autograder verify SUBMISSION.c --rules RULES.json [--runs 3]
    autograder check-rules RULES.json
    autograder tools
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import List, Optional

from . import tools
from .analyzers import REGISTRY
from .coordinator import grade
from .llm import LLMClient
from .report import write_reports
from .rules import Rules, RulesError, load_rules


def _load(rules_path: str) -> Rules:
    return load_rules(rules_path, REGISTRY)


def _llm(args) -> LLMClient:
    return LLMClient(Path(args.cache) / "llm" if args.cache else None, offline=args.offline)


def cmd_grade(args) -> int:
    rules = _load(args.rules)
    report = grade(Path(args.submission), rules, _llm(args), keep_workdir=args.keep)
    paths = write_reports(report, Path(args.out))
    if not args.quiet:
        sys.stdout.write(paths[1].read_text())
    print(f"\nwrote {paths[0]} and {paths[1]}", file=sys.stderr)
    return 0


def cmd_batch(args) -> int:
    rules = _load(args.rules)
    llm = _llm(args)
    files = sorted(Path(args.folder).glob("*.c"))
    if not files:
        print(f"no .c files in {args.folder}", file=sys.stderr)
        return 1
    out = Path(args.out)
    rows = []
    for f in files:
        report = grade(f, rules, llm)
        write_reports(report, out)
        flags = ",".join(sorted({fl["type"] for fl in report["flags"]}))
        rows.append({"submission": f.name, "grade": report["grade"],
                     **{m["name"]: m["score"] for m in report["metrics"]},
                     "flags": flags, "fingerprint": report["fingerprint"][7:23]})
        print(f"{f.name:<28} {report['grade']:>6.1f}  {flags}", file=sys.stderr)
    with open(out / "summary.csv", "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} reports and {out / 'summary.csv'}", file=sys.stderr)
    return 0


def cmd_verify(args) -> int:
    """Grade the same submission N times and require identical fingerprints."""
    rules = _load(args.rules)
    llm = _llm(args)
    prints = []
    for i in range(args.runs):
        r = grade(Path(args.submission), rules, llm)
        prints.append(r["fingerprint"])
        print(f"run {i + 1}: grade {r['grade']:.1f}  {r['fingerprint']}  "
              f"({r['meta']['elapsed_s']}s, llm cache hits {r['meta']['llm_cache']['hits']})")
    if len(set(prints)) == 1:
        print(f"REPRODUCIBLE: {args.runs} runs produced identical reports")
        return 0
    print("NOT REPRODUCIBLE: reports differ between runs")
    return 2


def cmd_check_rules(args) -> int:
    rules = _load(args.rules)
    total = sum(m.weight for m in rules.metrics)
    print(f"OK: {rules.problem} - {len(rules.metrics)} metrics, weights sum to {total:.2f}")
    for m in rules.metrics:
        print(f"  {m.name:<15} {m.weight:>5.2f}  {m.analyzer}")
    return 0


def cmd_tools(args) -> int:
    snap = tools.snapshot(args.cc)
    for k, v in snap.items():
        print(f"{k:<10} {v or 'NOT FOUND'}")
    print("analyzers  " + ", ".join(sorted(REGISTRY)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autograder", description="Rule-driven grader for C programs.")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, needs_rules=True):
        if needs_rules:
            sp.add_argument("--rules", required=True, help="rules JSON file")
        sp.add_argument("--cache", default=None, help="cache folder (default: $AUTOGRADER_CACHE or .autograder-cache)")
        sp.add_argument("--offline", action="store_true", help="never call an LLM; use cached answers only")

    g = sub.add_parser("grade", help="grade one submission")
    g.add_argument("submission")
    g.add_argument("--out", default="reports")
    g.add_argument("--keep", action="store_true", help="keep the temporary build folder")
    g.add_argument("--quiet", action="store_true")
    common(g)
    g.set_defaults(func=cmd_grade)

    b = sub.add_parser("batch", help="grade every .c file in a folder")
    b.add_argument("folder")
    b.add_argument("--out", default="reports")
    common(b)
    b.set_defaults(func=cmd_batch)

    v = sub.add_parser("verify", help="grade N times and check the reports are identical")
    v.add_argument("submission")
    v.add_argument("--runs", type=int, default=3)
    common(v)
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("check-rules", help="validate a rules file")
    c.add_argument("rules")
    c.set_defaults(func=cmd_check_rules)

    t = sub.add_parser("tools", help="show tool versions")
    t.add_argument("--cc", default="gcc")
    t.set_defaults(func=cmd_tools)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RulesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
