"""Built-in analyzers. Importing this package registers all of them."""

from . import (  # noqa: F401  (imported for their @register side effect)
    compiler_warnings,
    complexity_estimator,
    edge_case_runner,
    function_split,
    io_tests,
    llm_review,
    sanitizer_run,
    similarity_flag,
    source_patterns,
    static_analysis,
)
from .base import REGISTRY, Analyzer, AnalyzerResult, Counterexample, register

__all__ = ["REGISTRY", "Analyzer", "AnalyzerResult", "Counterexample", "register"]
