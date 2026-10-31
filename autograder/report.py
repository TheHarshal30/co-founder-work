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


def render_text(r: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append(f"===== Grading report for {r['submission']} =====")
    L.append(f"Problem: {r['problem']}")
    status = "" if r["grade_complete"] else "  (INCOMPLETE - see flags)"
    if r.get("partial"):
        status += "  (PARTIAL REPORT)"
    L.append(f"Overall grade: {r['grade']:.1f} / 100{status}")
    L.append("")
    L.append(f"{'Metric':<15}{'Weight':>6}  {'Score':>8}  Verdict")
    for m in r["metrics"]:
        verdict = m["feedback"]
        prefix = f"{m['name']:<15}{round(m['weight'] * 100):>6}  {_score_text(m):>8}  "
        wrapped = textwrap.wrap(verdict, WIDTH - len(prefix)) or [""]
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

    prep = r.get("asset_preparation")
    if prep:
        L.append("")
        L.append("--- Test assets (generated workflow) ---")
        L.extend(_wrap("Expected outputs from: " + prep["expected_outputs_from"].replace("_", " ")
                       + ". The submission's own output is never used as the answer key.", 2, "  "))
        if prep.get("assets"):
            L.append("  Assets: " + ", ".join(f"{k} {v}" for k, v in prep["assets"].items()))
        for name, m in prep["metrics"].items():
            L.extend(_wrap(f"{name}: {m['status'].replace('_', ' ')} - {m['detail']}", 4, "  - "))

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
    L.append("Tools: " + ", ".join(f"{k} {v}" for k, v in tv.items() if v and k not in ("cc",)) )
    L.append(f"Compiler: {tv.get('cc')}")
    return "\n".join(L) + "\n"
