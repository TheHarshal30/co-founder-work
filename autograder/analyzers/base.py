"""The analyzer contract and the plugin registry.

An analyzer judges one metric. It receives the shared grading context and the
``params`` slice of its metric from the rules file, and returns a score in
[0, 1] plus evidence, feedback and (optionally) counterexamples. The coordinator
never needs to know what an analyzer does internally, which is what keeps the
set of metrics open: a new metric is a new file here plus a new rules entry.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional, Type

if TYPE_CHECKING:  # pragma: no cover
    from ..context import Context
    from ..rules import Rules

REGISTRY: Dict[str, Type["Analyzer"]] = {}


def register(cls: Type["Analyzer"]) -> Type["Analyzer"]:
    REGISTRY[cls.name] = cls
    return cls


@dataclass
class Counterexample:
    label: str  # "test 07", "edge case 'all_negative'", ...
    input: str
    expected: str
    got: str
    verdict: str  # wrong_answer | presentation_error | timeout | runtime_error | ...
    why: str  # what went wrong on this input
    fix: str = ""  # what to change (input-independent, so suggestions de-duplicate)
    diagnostic: Optional[str] = None  # sanitizer finding on this input, if any
    # Text for the "suggestions" list. Rule-based (so identical mistakes merge)
    # unless the rule-based fix is generic and an LLM gave a better one.
    suggestion: str = field(default="", repr=False)

    def as_dict(self) -> Dict[str, Any]:
        d = {"label": self.label, "input": self.input, "expected": self.expected,
             "got": self.got, "verdict": self.verdict, "why": self.why, "fix": self.fix}
        if self.diagnostic:
            d["diagnostic"] = self.diagnostic
        return d


@dataclass
class AnalyzerResult:
    score: Optional[float]  # 0..1, or None when the metric could not be measured
    feedback: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    suggestions: List[str] = field(default_factory=list)
    counterexamples: List[Counterexample] = field(default_factory=list)
    flags: List[Dict[str, Any]] = field(default_factory=list)
    # ok | skipped (student's fault, scores 0) | unavailable (tool missing) | error
    status: str = "ok"

    @classmethod
    def unavailable(cls, reason: str, **evidence) -> "AnalyzerResult":
        return cls(score=None, feedback=f"Not measured: {reason}", evidence=evidence,
                   status="unavailable")


class Analyzer:
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    # True if the analyzer has to execute the student's binary; such analyzers
    # are skipped when the first pass shows the program does not compile/halt.
    needs_execution: ClassVar[bool] = False

    def validate(self, params: Dict[str, Any], rules: "Rules") -> List[str]:
        """Return human-readable problems with ``params`` (checked at load time,
        so a typo in the rules file fails fast instead of mid-grading)."""
        return []

    def run(self, ctx: "Context", params: Dict[str, Any]) -> AnalyzerResult:
        raise NotImplementedError


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def load_plugin_file(path: Path) -> None:
    """Import an extra analyzer module named in the rules (``"plugins": [...]``).
    Its ``@register`` decorators add it to the registry."""
    spec = importlib.util.spec_from_file_location(f"autograder_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
