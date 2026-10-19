"""Instructor-defined required / forbidden code patterns (regex on the source
with comments removed). Shows how a new metric can be added from the rules file
alone, e.g. "must not use gets()", "must use recursion", "no goto".

params:
  forbid:  [{"pattern": "\\bgets\\s*\\(", "message": "gets() is unsafe; use fgets()"}]
  require: [{"pattern": "\\bmalloc\\s*\\(", "message": "allocate the array dynamically"}]
score = satisfied rules / all rules
"""

from __future__ import annotations

import re

from ..ctokens import strip_comments
from .base import Analyzer, AnalyzerResult, register


@register
class SourcePatterns(Analyzer):
    name = "source_patterns"
    description = "Required/forbidden source patterns"
    needs_execution = False

    def validate(self, params, rules):
        problems = []
        for key in ("forbid", "require"):
            for i, item in enumerate(params.get(key, [])):
                if not isinstance(item, dict) or "pattern" not in item:
                    problems.append(f"{key}[{i}] needs a 'pattern'")
                    continue
                try:
                    re.compile(item["pattern"])
                except re.error as e:
                    problems.append(f"{key}[{i}] bad regex: {e}")
        if not params.get("forbid") and not params.get("require"):
            problems.append("give at least one 'forbid' or 'require' pattern")
        return problems

    def run(self, ctx, params):
        code = strip_comments(ctx.source)
        results, suggestions = [], []
        for key in ("forbid", "require"):
            for item in params.get(key, []):
                hits = [code.count("\n", 0, m.start()) + 1 for m in re.finditer(item["pattern"], code)]
                ok = (not hits) if key == "forbid" else bool(hits)
                msg = item.get("message", f"{key} /{item['pattern']}/")
                results.append({"rule": key, "pattern": item["pattern"], "ok": ok, "lines": hits[:10]})
                if not ok:
                    where = f" (line {', '.join(map(str, hits[:5]))})" if hits else ""
                    suggestions.append(msg + where)
        passed = sum(r["ok"] for r in results)
        feedback = (f"All {len(results)} code rules satisfied." if passed == len(results)
                    else f"{len(results) - passed} code rule(s) violated: " + "; ".join(suggestions[:2]))
        return AnalyzerResult(score=passed / len(results), feedback=feedback,
                              evidence={"rules": results}, suggestions=suggestions)
