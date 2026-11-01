"""Write the machine report (JSON) and the student report (text)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Dict, List

WIDTH = 78


def write_reports(report: Dict[str, Any], out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(report["submission"]).stem
    json_path, txt_path = out_dir / f"{stem}.json", out_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    txt_path.write_text(render_text(report))
    return [json_path, txt_path]


def _wrap(text: str, indent: int, first: str = "") -> List[str]:
    pad = " " * indent
    return textwrap.wrap(text, WIDTH, initial_indent=first or pad, subsequent_indent=pad) or [first or pad]


def _block(label: str, value: str, indent: int = 9) -> List[str]:
    """'INPUT:   ...' with multi-line values aligned under the first line."""
    lines = []
    for raw in (value.rstrip("\n").split("\n") if value else ["(empty)"]):
        lines.extend(textwrap.wrap(raw, WIDTH - indent - 2, break_on_hyphens=False) or [""])
    out = [f"{label:<{indent}}{lines[0]}"]
    out += [" " * indent + l for l in lines[1:]]
    return out


def _score_text(m: Dict[str, Any]) -> str:
    if m["score"] is None:
        return "n/a"
    full = round(m["weight"] * 100)
    got = m["score"] * m["weight"] * 100
    return f"{got:g}/{full}" if got == int(got) else f"{got:.1f}/{full}"


def _status(m: Dict[str, Any]) -> str:
    return str(m.get("status", "ok")).replace("_", " ").upper()


def _findings(m: Dict[str, Any]) -> List[str]:
    """One-line summaries of what each analyzer measured (its evidence)."""
    e, a = m.get("evidence") or {}, m["analyzer"]
    out: List[str] = []
    if a == "io_tests" and "tests_total" in e:
        out.append(f"{e['tests_passed']}/{e['tests_total']} tests passed")
    elif a == "edge_case_runner" and e.get("families"):
        out.append("corner cases: " + ", ".join(f"{f['family']} {f['passed']}/{f['total']}" for f in e["families"]))
    elif a == "complexity_estimator" and e.get("measurements") is not None:
        series = ", ".join(f"n={p['n']}: {p['cost']:,}" for p in e["measurements"])
        est = e.get("estimated", "not estimated")
        out.append(f"estimated {est}, required {e.get('required')} ({e.get('method')}: {series})")
        if e.get("failed_at_size"):
            out.append(f"failed at size {e['failed_at_size']}")
    elif a == "compiler_warnings" and "warnings" in e:
        out.append(f"{len(e['warnings'])} warning(s), {len(e.get('errors', []))} error(s) with {e.get('flags')}")
        out += [f"line {w.get('line')}: {w.get('message')}" for w in e["warnings"][:3]]
    elif a == "function_split" and "functions_defined" in e:
        out.append(f"{e['functions_defined']} function(s) (expected at least {e.get('expected_min')})")
    elif a == "sanitizer_run" and "inputs_checked" in e:
        out.append(f"{e['inputs_checked']} input(s) run under {e.get('sanitizers') or 'sanitizers'}; "
                   f"{len(e.get('issues') or [])} issue(s)")
    elif a == "static_analysis" and e.get("deductions"):
        out += [f"-{d['points']:g}: {d['reason']}" for d in e["deductions"][:4]]
    elif a == "similarity_flag" and "fingerprints" in e:
        verdict = str(e.get("verdict", "")).replace("_", " ").upper()
        line = (f"{verdict}: {e['fingerprints']} distinctive fingerprints (minimum {e.get('min_fingerprints', 8)}), "
                f"compared against {e.get('compared_against', 0)} file(s)")
        top = (e.get("top_matches") or [None])[0]
        if top:
            line += (f"; closest {top['file']} at {top['similarity']:.2f} sharing "
                     f"{top.get('shared_fingerprints', '?')} fingerprints")
        out.append(line)
    return out


def render_text(r: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append(f"===== Grading report for {r['submission']} =====")
    L.append(f"Problem: {r['problem']}")
    status = "" if r["grade_complete"] else "  (INCOMPLETE - see flags)"
    if r.get("partial"):
        status += "  (PARTIAL REPORT)"
    L.append(f"Overall grade: {r['grade']:.1f} / 100{status}")
    L.append("")
    L.append(f"{'Metric':<15}{'Weight':>6}  {'Score':>8}  {'Status':<14}Verdict")
    for m in r["metrics"]:
        verdict = m["feedback"]
        prefix = f"{m['name']:<15}{round(m['weight'] * 100):>6}  {_score_text(m):>8}  {_status(m):<14}"
        wrapped = textwrap.wrap(verdict, max(30, WIDTH - len(prefix))) or [""]
        L.append(prefix + wrapped[0])
        L.extend(" " * len(prefix) + w for w in wrapped[1:5])

    if r["flags"]:
        L.append("")
        L.append("--- Flags ---")
        for f in r["flags"]:
            L.extend(_wrap(f"[{f['type']}] {f['detail']}", 2, "- "))

    if r["counterexamples"]:
        L.append("")
        L.append("--- Where you failed ---")
        for c in r["counterexamples"]:
            L.append(f"{c['label']}  ({c['metric']}, {c['verdict'].replace('_', ' ')})")
            for label, key in (("INPUT:", "input"), ("EXPECTED:", "expected"), ("GOT:", "got")):
                L.extend("  " + x for x in _block(label, c[key], 10))
            L.extend(_wrap(c["why"], 12, "  WHY:      "))
            if c.get("fix"):
                L.extend(_wrap(c["fix"], 12, "  FIX:      "))
            if c.get("same_problem_on"):
                L.extend(_wrap("same problem on " + ", ".join(c["same_problem_on"]), 12, "  ALSO:     "))
            L.append("")
        if L[-1] == "":
            L.pop()

    issues = next((m["evidence"].get("issues") for m in r["metrics"]
                   if m["analyzer"] == "sanitizer_run" and m["evidence"].get("issues")), None)
    if issues:
        L.append("")
        L.append("--- Memory / undefined-behaviour findings ---")
        for i in issues:
            L.extend(_wrap(f"{i['location'] or '?'}: {i['message']} [{i['tool']}]", 4, "- "))
            L.extend(_wrap(f"triggered by input: {i['input']!r}", 4))

    if r["suggestions"]:
        L.append("")
        L.append("--- Suggestions to raise the grade (biggest gains first) ---")
        for s in r["suggestions"]:
            L.extend(_wrap(f"{s['suggestion']} [{s['metric']}]", 2, "- "))

    findings = [(m["name"], f) for m in r["metrics"] for f in _findings(m)]
    if findings:
        L.append("")
        L.append("--- Analyzer findings ---")
        for name, f in findings:
            L.extend(_wrap(f"{name}: {f}", 4, "  - "))

    prep = r.get("asset_preparation") or {}
    not_run = [m for m in r["metrics"] if m["status"] not in ("ok",)]
    asset_skips = [(n, s) for n, m in prep.get("metrics", {}).items() for s in m.get("skipped", [])]
    if not_run or asset_skips:
        L.append("")
        L.append("--- Not performed / skipped, and why ---")
        for m in not_run:
            L.extend(_wrap(f"{m['name']} ({_status(m).strip()}): {m['feedback']}", 4, "  - "))
        for name, why in asset_skips:
            L.extend(_wrap(f"{name}: skipped {why}", 4, "  - "))

    if prep:
        L.append("")
        L.append("--- Test assets (generated workflow) ---")
        L.extend(_wrap("Expected outputs from: " + prep["expected_outputs_from"].replace("_", " ")
                       + ". The submission's own output is never used as the answer key.", 2, "  "))
        if prep.get("storage"):
            L.extend(_wrap(f"Stored in: <cache>/{prep['storage']} (generation {prep.get('generation_id', '')})",
                           2, "  "))
        if prep.get("spec_basis"):
            L.extend(_wrap(f"Input format: {prep['spec_basis']}", 2, "  "))
        oracle = prep.get("oracle") or {}
        if oracle.get("reason"):
            L.extend(_wrap(f"Oracle ({oracle.get('mode', '').replace('_', ' ')}): {oracle['reason']}", 2, "  "))
        for c in oracle.get("candidates", []):
            L.extend(_wrap(f"candidate {c['label']}: {c['status']}" + (f" - {c['detail']}" if c.get("detail") else ""),
                           6, "    - "))
        if prep.get("assets"):
            L.append("  Assets: " + ", ".join(f"{k} {v}" for k, v in prep["assets"].items()))
        for name, m in prep["metrics"].items():
            L.extend(_wrap(f"{name}: {m['status'].replace('_', ' ')} - {m['detail']}", 4, "  - "))
        for f in prep.get("failures", []):
            L.extend(_wrap(f"generation failure (not the student's fault): {f['step']}: {f['detail']}", 4, "  - "))

    ul = r.get("usefulness_limitations")
    if ul:
        L.append("")
        L.append("--- Usefulness & limitations of this evaluation ---")
        L.append("Useful for:")
        for i, u in enumerate(ul["usefulness"], 1):
            L.extend(_wrap(u["use_case"], 5, f"  {i}. "))
            L.extend(_wrap("Example: " + u["example"], 5))
            L.extend(_wrap("Benefit: " + u["benefit"], 5))
        L.append("Limits of this report:")
        for i, u in enumerate(ul["limitations"], 1):
            L.extend(_wrap(u["limitation"], 5, f"  {i}. "))
            L.extend(_wrap("Example: " + u["example"], 5))
            L.extend(_wrap("Fix: " + u["mitigation"], 5))

    L.append("")
    L.append(f"Report fingerprint: {r['fingerprint']}")
    L.append(f"Rules: {r['rules_hash'][:23]}...  Assets: {r['assets_hash'][:23]}...")
    tv = r["tool_versions"]
    L.append("Tools: " + ", ".join(f"{k} {v}" for k, v in sorted(tv.items()) if v and k not in ("cc",)))
    L.append(f"Compiler: {tv.get('cc')}")
    return "\n".join(L) + "\n"
