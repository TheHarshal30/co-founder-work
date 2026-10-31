"""Command line: grade one file, grade a folder, verify reproducibility, check rules.

    autograder grade  SUBMISSION.c --rules RULES.json [--out reports/] [--generate-assets]
    autograder batch  FOLDER       --rules RULES.json [--out reports/] [--generate-assets]
    autograder verify SUBMISSION.c --rules RULES.json [--runs 3]       [--generate-assets]
    autograder prepare --rules RULES.json      generate + validate missing assets only
    autograder check-rules RULES.json
    autograder tools

Without --generate-assets nothing changes: every asset the rules name must exist.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import tools
from .analyzers import REGISTRY
from .assets import AssetStore, Preparation, prepare_assets
from .coordinator import grade
from .llm import LLMClient
from .report import write_reports
from .rules import Rules, RulesError, load_rules

DB_NAME = "autograder.sqlite3"


def _cache_root(args) -> Path:
    return Path(args.cache or os.environ.get("AUTOGRADER_CACHE", ".autograder-cache"))


def _llm(args) -> LLMClient:
    return LLMClient(Path(args.cache) / "llm" if args.cache else None, offline=args.offline)


def _store(args) -> Optional[AssetStore]:
    if getattr(args, "no_db", False):
        return None
    try:
        return AssetStore(_cache_root(args) / DB_NAME)
    except (sqlite3.Error, OSError) as e:
        print(f"warning: database unavailable ({e}); grades are written to files only", file=sys.stderr)
        return None


def _prepare(args, llm: LLMClient) -> Preparation:
    store = _store(args)
    if store is None:  # generated assets are only trusted once they are recorded
        store = AssetStore(_cache_root(args) / DB_NAME)
    try:
        prep = prepare_assets(args.rules, llm, store, _cache_root(args))
    finally:
        store.close()
    if prep.overlay is not None:
        _print_preparation(prep)
    return prep


def _load(args, llm: Optional[LLMClient] = None) -> Rules:
    if getattr(args, "generate_assets", False):
        prep = _prepare(args, llm or _llm(args))
        args._prep = prep
        return load_rules(args.rules, REGISTRY, prep.overlay)
    return load_rules(args.rules, REGISTRY)


def _print_preparation(prep: Preparation) -> None:
    sec = prep.overlay.report if prep.overlay else {}
    err = sys.stderr
    print(f"asset preparation: bundle {prep.bundle_key[:16] if prep.bundle_key else '-'} "
          f"({'cached' if prep.cache_hit else 'generated now'}) in {prep.bundle_dir}", file=err)
    counts = sec.get("assets", {})
    if counts:
        print("  assets: " + ", ".join(f"{k} {v}" for k, v in counts.items()), file=err)
    print(f"  expected outputs from: {sec.get('expected_outputs_from', '-').replace('_', ' ')}", file=err)
    for name, m in sec.get("metrics", {}).items():
        print(f"  {name:<15} {m['status']:<14} {m['detail']}", file=err)
    for f in sec.get("failures", []):
        print(f"  generation failure: {f['step']}: {f['detail']}", file=err)


def _record(args, store: Optional[AssetStore], report: Dict[str, Any]) -> None:
    prep = getattr(args, "_prep", None)
    if prep is not None and prep.overlay is not None:
        report["meta"]["assets"] = {"cache_hit": prep.cache_hit, "bundle_dir": str(prep.bundle_dir)}
    if store is None:
        return
    try:
        store.record_grade(report)
    except sqlite3.Error as e:
        print(f"warning: could not record the grade in the database ({e})", file=sys.stderr)


def cmd_grade(args) -> int:
    llm = _llm(args)
    rules = _load(args, llm)
    report = grade(Path(args.submission), rules, llm, keep_workdir=args.keep)
    store = _store(args)
    _record(args, store, report)
    paths = write_reports(report, Path(args.out))
    if not args.quiet:
        sys.stdout.write(paths[1].read_text())
    print(f"\nwrote {paths[0]} and {paths[1]}", file=sys.stderr)
    return 0


def cmd_batch(args) -> int:
    llm = _llm(args)
    rules = _load(args, llm)  # assets are prepared once for the whole batch
    store = _store(args)
    files = sorted(Path(args.folder).glob("*.c"))
    if not files:
        print(f"no .c files in {args.folder}", file=sys.stderr)
        return 1
    out = Path(args.out)
    rows = []
    for f in files:
        report = grade(f, rules, llm)
        _record(args, store, report)
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
    llm = _llm(args)
    rules = _load(args, llm)
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


def cmd_prepare(args) -> int:
    """Generate and validate the missing assets without grading anything."""
    prep = _prepare(args, _llm(args))
    if prep.overlay is None:
        print("nothing missing: every asset the rules need exists; the explicit workflow applies")
        return 0
    rules = load_rules(args.rules, REGISTRY, prep.overlay)  # the prepared rules must validate
    unperformed = rules.overlay.not_performed if rules.overlay else {}
    print(f"prepared {rules.problem}: {len(rules.metrics) - len(unperformed)}/{len(rules.metrics)} metrics "
          f"can be measured" + (f"; not performed: {', '.join(unperformed)}" if unperformed else ""))
    return 0


def cmd_check_rules(args) -> int:
    rules = load_rules(args.rules, REGISTRY)
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

    def common(sp, needs_rules=True, assets=True):
        if needs_rules:
            sp.add_argument("--rules", required=True, help="rules JSON file")
        sp.add_argument("--cache", default=None, help="cache folder (default: $AUTOGRADER_CACHE or .autograder-cache)")
        sp.add_argument("--offline", action="store_true", help="never call an LLM; use cached answers only")
        sp.add_argument("--no-db", action="store_true", help="do not record grades in the SQLite database")
        if assets:
            sp.add_argument("--generate-assets", action="store_true",
                            help="generate and validate test assets the rules need but the problem folder lacks")

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

    pr = sub.add_parser("prepare", help="generate + validate missing assets (no grading)")
    common(pr, assets=False)
    pr.set_defaults(func=cmd_prepare)

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
