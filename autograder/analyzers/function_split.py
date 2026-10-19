"""modularity: is the work split into functions, or is everything in main()?

score = 0.6 * min(1, functions in use / min_functions)
      + 0.4 * (main() is not doing most of the work)

"Functions in use" counts main() plus every helper that is actually called, so
a program with only main() and min_functions = 2 gets 0.6 * 1/2 = 0.3.

The second term uses main()'s share of all function lines: at or below
``max_main_share`` (default 0.8 -- main() legitimately keeps input, output and
error handling) it earns full marks, at 100% it earns none.
A helper that is defined but never called earns nothing.
"""

from __future__ import annotations

from .base import Analyzer, AnalyzerResult, clamp01, register
from .code_metrics import called_functions, functions


@register
class FunctionSplit(Analyzer):
    name = "function_split"
    description = "Decomposition into helper functions"
    needs_execution = False

    def validate(self, params, rules):
        if int(params.get("min_functions", 2)) < 1:
            return ["'min_functions' must be >= 1"]
        return []

    def run(self, ctx, params):
        min_functions = int(params.get("min_functions", 2))
        max_main_share = float(params.get("max_main_share", 0.8))
        funcs = functions(ctx.name, ctx.source)
        if not funcs:
            return AnalyzerResult(score=0.0, feedback="No functions found.", evidence={"functions_defined": 0})
        used_helpers = called_functions(ctx.source, funcs)
        main = next((f for f in funcs if f.name == "main"), None)
        total_lines = sum(f.nloc for f in funcs)
        main_share = (main.nloc / total_lines) if main and total_lines else 0.0

        wanted_helpers = max(0, min_functions - 1)
        in_use = len(used_helpers) + (1 if main else 0)
        helper_part = min(1.0, in_use / min_functions)
        if main_share <= max_main_share:
            main_part = 1.0
        else:
            main_part = clamp01((1.0 - main_share) / (1.0 - max_main_share))
        expect_helpers = params.get("expect_helpers", True)
        score = clamp01(0.6 * helper_part + 0.4 * main_part) if expect_helpers else 1.0

        unused = [f.name for f in funcs if f.name != "main" and f.name not in used_helpers]
        evidence = {
            "functions_defined": len(funcs), "expected_min": min_functions,
            "helpers_called": used_helpers, "helpers_never_called": unused,
            "main_share_of_lines": round(main_share, 2),
        }
        suggestions = []
        if len(funcs) == 1 and main:
            feedback = "Only main() is defined; all logic lives in one function."
            suggestions.append("Move the computation into its own function (e.g. a helper that takes "
                               "the inputs and returns the result) and call it from main().")
        elif helper_part < 1.0:
            feedback = (f"{len(used_helpers)} helper function(s) called; at least {wanted_helpers} expected.")
            suggestions.append("Split input parsing, the core computation and output into separate functions.")
        elif main_part < 1.0:
            feedback = f"main() still holds {main_share:.0%} of the code."
            suggestions.append("Keep main() short: read input, call helpers, print the result.")
        else:
            feedback = f"{len(funcs)} functions; main() delegates the work ({main_share:.0%} of the code)."
        if unused:
            suggestions.append(f"Remove or use the uncalled function(s): {', '.join(unused)}.")
        return AnalyzerResult(score=score, feedback=feedback, evidence=evidence, suggestions=suggestions)
