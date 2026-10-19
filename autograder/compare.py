"""Output comparison and the per-run verdict."""

from __future__ import annotations

import re
from typing import Tuple

from .sandbox import RunResult

MODES = ("exact", "trim", "tokens")


def _trim(s: str) -> str:
    lines = [l.rstrip() for l in s.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def outputs_match(expected: str, got: str, mode: str = "trim") -> bool:
    if mode == "exact":
        return expected == got
    if mode == "tokens":
        return expected.split() == got.split()
    if mode.startswith("float"):
        return _float_match(expected, got, mode)
    return _trim(expected) == _trim(got)


def _float_match(expected: str, got: str, mode: str) -> bool:
    m = re.match(r"float(?::(.+))?$", mode)
    eps = float(m.group(1)) if m and m.group(1) else 1e-6
    a, b = expected.split(), got.split()
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        try:
            fx, fy = float(x), float(y)
        except ValueError:
            if x != y:
                return False
            continue
        if abs(fx - fy) > eps * max(1.0, abs(fx)):
            return False
    return True


def verdict(run: RunResult, expected: str, mode: str = "trim",
            require_zero_exit: bool = True) -> Tuple[bool, str]:
    """(passed, verdict) for one test run."""
    if run.status in ("timeout", "runtime_error", "output_limit", "memory_limit"):
        return False, run.status
    if outputs_match(expected, run.stdout, mode):
        if run.status == "nonzero_exit" and require_zero_exit:
            return False, "nonzero_exit"
        return True, "pass"
    if expected.split() == run.stdout.split():
        return False, "presentation_error"
    return False, "wrong_answer"


def shorten(text: str, limit: int = 300) -> str:
    """Large inputs are summarised in reports; the full input stays reproducible
    through the test file / generator seed recorded alongside."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f" ... [{len(text)} bytes total]"
