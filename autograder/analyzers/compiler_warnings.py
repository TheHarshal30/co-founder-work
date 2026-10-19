"""syntax: compile with the instructor's warning flags and count diagnostics.

``-Werror`` is honoured for display but each warning-turned-error is still
counted as a warning, so -Werror changes nothing about the score -- only real
compile errors drop it to 0.
"""

from __future__ import annotations

from ..compiler import compile_c, split_flags
from .base import Analyzer, AnalyzerResult, clamp01, register


@register
class CompilerWarnings(Analyzer):
    name = "compiler_warnings"
    description = "Compiler warnings under strict flags"
    needs_execution = False

    def validate(self, params, rules):
        p = params.get("penalty_per_warning", 0.1)
        if not isinstance(p, (int, float)) or p < 0:
            return ["'penalty_per_warning' must be a number >= 0"]
        return []

    def run(self, ctx, params):
        flags = split_flags(params.get("flags", "-Wall -Wextra -std=c11"))
        # Several warnings (unused results, maybe-uninitialized) only fire when
        # the optimiser runs, so compile at -O2 unless the flags choose a level.
        if not any(f.startswith("-O") for f in flags):
            flags = flags + [params.get("optimize", "-O2")]
        build = compile_c(ctx.rules.cc, ctx.src_path, None, flags)
        warnings, errors = build.warnings, build.errors
        penalty = float(params.get("penalty_per_warning", 0.1))
        evidence = {
            "flags": " ".join(flags),
            "warnings": [d.as_dict() for d in warnings],
            "errors": [d.as_dict() for d in errors],
        }
        if errors:
            first = errors[0]
            return AnalyzerResult(
                score=0.0, feedback=f"Does not compile: line {first.line}: {first.message}",
                evidence=evidence, suggestions=[f"Fix the compile error at line {first.line}: {first.message}"],
            )
        if not warnings:
            return AnalyzerResult(score=1.0, feedback="No compiler warnings.", evidence=evidence)
        groups: dict = {}
        for w in warnings:
            groups.setdefault((w.message, w.flag), []).append(w.line)
        described = []
        for (message, flag), lines in groups.items():
            where = ("line " if len(lines) == 1 else "lines ") + ", ".join(map(str, lines))
            described.append(f"{where}: {message}" + (f" [{flag}]" if flag else ""))
        return AnalyzerResult(
            score=clamp01(1.0 - penalty * len(warnings)),
            feedback=f"{len(warnings)} warning(s): " + "; ".join(described[:2]),
            evidence=evidence,
            suggestions=[f"Fix the warning at {d}" for d in described[:3]],
        )
