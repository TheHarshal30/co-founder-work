"""Example plugin: a metric added without touching the agent's source.

Reference it from a rules file:

    "plugins": ["../../examples/plugins/line_budget.py"],
    "metrics": [ ...,
      { "name": "conciseness", "analyzer": "line_budget", "weight": 0.05,
        "params": { "max_code_lines": 30 } } ]
"""

from autograder.analyzers import Analyzer, AnalyzerResult, register
from autograder.ctokens import readability_facts


@register
class LineBudget(Analyzer):
    name = "line_budget"
    description = "Code lines within a budget (comments and blank lines are free)"
    needs_execution = False

    def validate(self, params, rules):
        if int(params.get("max_code_lines", 0)) <= 0:
            return ["'max_code_lines' must be a positive integer"]
        return []

    def run(self, ctx, params):
        limit = int(params["max_code_lines"])
        lines = readability_facts(ctx.source)["code_lines"]
        score = 1.0 if lines <= limit else max(0.0, 1 - (lines - limit) / limit)
        return AnalyzerResult(
            score=score,
            feedback=f"{lines} code lines (budget {limit}).",
            evidence={"code_lines": lines, "budget": limit},
            suggestions=[] if score == 1.0 else [f"Trim the program to about {limit} lines of code."],
        )
