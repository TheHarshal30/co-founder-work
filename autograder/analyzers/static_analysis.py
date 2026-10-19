"""structure: function size/complexity limits (lizard), static bug finding
(cppcheck), mutable globals and goto.

Score starts at 1.0 and loses a fixed, documented amount per finding, so a
student can see exactly which finding cost how much.
"""

from __future__ import annotations

import subprocess
from typing import List

from .. import tools
from ..ctokens import C_KEYWORDS, tokenize
from .base import Analyzer, AnalyzerResult, clamp01, register
from .code_metrics import functions

COUNTED_SEVERITIES = ("error", "warning", "performance", "portability")
IGNORED_IDS = {"missingIncludeSystem", "missingInclude", "checkersReport", "unusedFunction",
               "normalCheckLevelMaxBranches", "toomanyconfigs", "unmatchedSuppression"}


def mutable_globals(source: str) -> List[dict]:
    """File-scope variables that are not const (shared mutable state)."""
    toks = tokenize(source)
    found, stmt, depth = [], [], 0
    for t in toks:
        if t.kind in ("pp", "comment"):
            continue
        if t.text == "{":
            if depth == 0:
                stmt = []  # function or struct body follows: not a variable declaration
            depth += 1
            continue
        if t.text == "}":
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        if t.text == ";":
            texts = [x.text for x in stmt]
            head = stmt[: texts.index("=")] if "=" in texts else stmt
            # a declaration (type + name), not a prototype, not const/extern/typedef
            if len(head) >= 2 and "(" not in [x.text for x in head] \
                    and not ({"typedef", "extern", "const"} & set(texts)):
                names = [x for x in head if x.kind == "id" and x.text not in C_KEYWORDS]
                if names:
                    found.append({"name": names[-1].text, "line": stmt[0].line})
            stmt = []
        else:
            stmt.append(t)
    return found


def run_cppcheck(ctx) -> List[dict]:
    exe = tools.which("cppcheck")
    if not exe:
        raise FileNotFoundError("cppcheck")
    cmd = [exe, "--enable=warning,style,performance,portability", "--std=c11", "--language=c",
           "--template={line}|{severity}|{id}|{message}", "--quiet", "--inline-suppr", ctx.name]
    proc = subprocess.run(cmd, cwd=str(ctx.src_dir), capture_output=True, text=True, timeout=120)
    findings = []
    for line in (proc.stderr + proc.stdout).splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4 or not parts[0].isdigit():
            continue
        lineno, severity, ident, message = parts
        if ident in IGNORED_IDS or severity == "information":
            continue
        findings.append({"line": int(lineno), "severity": severity, "id": ident, "message": message})
    findings.sort(key=lambda f: (f["line"], f["id"]))
    return findings


@register
class StaticAnalysis(Analyzer):
    name = "static_analysis"
    description = "Function length/complexity limits, cppcheck findings, globals, goto"
    needs_execution = False

    def run(self, ctx, params):
        max_ccn = int(params.get("max_cyclomatic", 10))
        max_len = int(params.get("max_function_length", 50))
        p_limit = float(params.get("penalty_per_violation", 0.2))
        p_tool = float(params.get("penalty_per_finding", 0.1))
        p_global = float(params.get("penalty_per_global", 0.1))

        funcs = functions(ctx.name, ctx.source)
        deductions, suggestions = [], []
        for f in funcs:
            if f.cyclomatic > max_ccn:
                deductions.append((p_limit, f"{f.name}() has cyclomatic complexity {f.cyclomatic} "
                                            f"(limit {max_ccn})"))
                suggestions.append(f"Split {f.name}() (lines {f.start_line}-{f.end_line}) into smaller "
                                   f"functions; it has {f.cyclomatic} independent paths.")
            if f.nloc > max_len:
                deductions.append((p_limit, f"{f.name}() is {f.nloc} lines long (limit {max_len})"))
                suggestions.append(f"Shorten {f.name}() to at most {max_len} lines by extracting helpers.")

        globals_found = mutable_globals(ctx.source)
        for g in globals_found:
            deductions.append((p_global, f"mutable global variable '{g['name']}' (line {g['line']})"))
        if globals_found:
            suggestions.append("Pass data through function parameters instead of global variables "
                               f"({', '.join(g['name'] for g in globals_found)}).")

        gotos = [t.line for t in tokenize(ctx.source) if t.kind == "id" and t.text == "goto"]
        for line in gotos:
            deductions.append((p_global, f"goto at line {line}"))

        evidence = {"limits": {"max_cyclomatic": max_ccn, "max_function_length": max_len},
                    "functions": [f.as_dict() for f in funcs], "mutable_globals": globals_found,
                    "goto_lines": gotos}
        if params.get("cppcheck", True):
            try:
                findings = run_cppcheck(ctx)
                counted = [f for f in findings if f["severity"] in COUNTED_SEVERITIES]
                for f in counted:
                    weight = 2 * p_tool if f["severity"] == "error" else p_tool
                    deductions.append((weight, f"cppcheck {f['severity']} line {f['line']}: {f['message']}"))
                    suggestions.append(f"Line {f['line']}: {f['message']} (cppcheck {f['id']})")
                evidence["cppcheck"] = {"counted": counted,
                                        "advisory": [f for f in findings if f not in counted]}
            except (FileNotFoundError, subprocess.TimeoutExpired):
                evidence["cppcheck"] = "unavailable"

        score = clamp01(1.0 - sum(d for d, _ in deductions))
        evidence["deductions"] = [{"points": round(d, 3), "reason": r} for d, r in deductions]
        if not deductions:
            feedback = (f"{len(funcs)} function(s), all within complexity {max_ccn} and "
                        f"length {max_len}; no static-analysis findings.")
        else:
            feedback = "; ".join(r for _, r in deductions[:3]) + (
                f" (+{len(deductions) - 3} more)" if len(deductions) > 3 else "")
        return AnalyzerResult(score=score, feedback=feedback, evidence=evidence, suggestions=suggestions)
