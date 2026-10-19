"""Function-level facts from lizard, shared by the structure and modularity
analyzers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from ..ctokens import strip_comments


@dataclass
class FunctionInfo:
    name: str
    start_line: int
    end_line: int
    nloc: int
    cyclomatic: int
    parameters: int
    nesting: int

    def as_dict(self) -> dict:
        return {"name": self.name, "lines": f"{self.start_line}-{self.end_line}", "nloc": self.nloc,
                "cyclomatic": self.cyclomatic, "parameters": self.parameters}


def functions(name: str, source: str) -> List[FunctionInfo]:
    import lizard  # type: ignore

    info = lizard.analyze_file.analyze_source_code(name, source)
    return [
        FunctionInfo(f.name, f.start_line, f.end_line, f.nloc, f.cyclomatic_complexity,
                     f.parameter_count, getattr(f, "top_nesting_level", 0))
        for f in info.function_list
    ]


def called_functions(source: str, funcs: List[FunctionInfo]) -> List[str]:
    """Names of user-defined functions that are called from another function
    (a helper nobody calls does not make a program modular)."""
    lines = strip_comments(source).splitlines()
    called = []
    for f in funcs:
        if f.name == "main":
            continue
        pattern = re.compile(r"\b" + re.escape(f.name) + r"\s*\(")
        for g in funcs:
            if g is f:
                continue
            body = "\n".join(lines[g.start_line - 1:g.end_line])
            if pattern.search(body):
                called.append(f.name)
                break
    return called
