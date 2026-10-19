"""Load and validate the instructor's rules file.

Everything an instructor may want to change -- which metrics, their weights,
thresholds, limits, test folders -- lives in this JSON file, never in code.
Relative paths inside it are resolved against the rules file's own folder, so a
problem folder (rules + tests + reference solution) can be moved around freely.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .compiler import split_flags

WEIGHT_TOLERANCE = 1e-6


class RulesError(ValueError):
    def __init__(self, path: Path, problems: List[str]):
        self.path = path
        self.problems = problems
        super().__init__(f"invalid rules file {path}:\n  - " + "\n  - ".join(problems))


@dataclass
class Metric:
    name: str
    analyzer: str
    weight: float
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Rules:
    path: Path
    problem: str
    time_limit_s: float
    memory_limit_mb: int
    metrics: List[Metric]
    cc: str
    compile_flags: List[str]
    ldflags: List[str]
    sample_input: Optional[str]
    reference: Optional[Path]
    statement: Optional[Path]
    include_usefulness_limitations: bool
    raw: Dict[str, Any]
    sha256: str

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)

    def statement_text(self) -> str:
        if self.statement and self.statement.exists():
            return self.statement.read_text(errors="replace")
        return ""


def load_rules(path, registry=None) -> Rules:
    """Parse and validate. ``registry`` (name -> analyzer class) enables
    per-analyzer parameter checks; pass ``None`` to skip them."""
    path = Path(path).resolve()
    try:
        text = path.read_text()
    except OSError as e:
        raise RulesError(path, [f"cannot read file: {e}"])
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise RulesError(path, [f"not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})"])
    if not isinstance(raw, dict):
        raise RulesError(path, ["top level must be a JSON object"])

    problems: List[str] = []

    def num(key, default, minimum=0.0):
        v = raw.get(key, default)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= minimum:
            problems.append(f"'{key}' must be a number > {minimum}")
            return default
        return v

    problem = raw.get("problem")
    if not isinstance(problem, str) or not problem:
        problems.append("'problem' must be a non-empty string")
        problem = "unknown"
    time_limit = float(num("time_limit_s", 1))
    memory_limit = int(num("memory_limit_mb", 256))

    comp = raw.get("compile", {})
    if not isinstance(comp, dict):
        problems.append("'compile' must be an object")
        comp = {}
    cc = comp.get("cc", "gcc")
    compile_flags = split_flags(comp.get("flags", "-O2 -std=c11"))
    ldflags = split_flags(comp.get("ldflags", "-lm"))

    base = path.parent
    reference = None
    if raw.get("reference") is not None:
        reference = (base / raw["reference"]).resolve()
        if not reference.exists():
            problems.append(f"reference solution not found: {raw['reference']}")
    statement = (base / raw["statement"]).resolve() if raw.get("statement") else None
    sample_input = raw.get("sample_input")
    if sample_input is not None and not isinstance(sample_input, str):
        problems.append("'sample_input' must be a string")
        sample_input = None

    metrics: List[Metric] = []
    raw_metrics = raw.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        problems.append("'metrics' must be a non-empty list")
        raw_metrics = []
    seen = set()
    for i, m in enumerate(raw_metrics):
        where = f"metrics[{i}]"
        if not isinstance(m, dict):
            problems.append(f"{where} must be an object")
            continue
        name, analyzer, weight = m.get("name"), m.get("analyzer"), m.get("weight")
        params = m.get("params", {})
        if not isinstance(name, str) or not name:
            problems.append(f"{where}: 'name' must be a non-empty string")
            continue
        where = f"metric '{name}'"
        if name in seen:
            problems.append(f"{where}: duplicate metric name")
        seen.add(name)
        if not isinstance(analyzer, str):
            problems.append(f"{where}: 'analyzer' must be a string")
            continue
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight < 0:
            problems.append(f"{where}: 'weight' must be a number >= 0")
            continue
        if not isinstance(params, dict):
            problems.append(f"{where}: 'params' must be an object")
            continue
        metrics.append(Metric(name, analyzer, float(weight), params))

    total = sum(m.weight for m in metrics)
    if metrics and abs(total - 1.0) > WEIGHT_TOLERANCE:
        problems.append(f"weights must sum to 1.00 (they sum to {total:.4f})")

    rules = Rules(
        path=path, problem=problem, time_limit_s=time_limit, memory_limit_mb=memory_limit,
        metrics=metrics, cc=cc, compile_flags=compile_flags, ldflags=ldflags,
        sample_input=sample_input, reference=reference, statement=statement,
        include_usefulness_limitations=bool(raw.get("include_usefulness_limitations", True)),
        raw=raw, sha256=hashlib.sha256(text.encode()).hexdigest(),
    )

    if registry is not None:
        # Extra analyzers shipped next to the rules ("plugins": ["my_metric.py"])
        # register themselves on import, so a new metric needs no agent edits.
        from .analyzers.base import load_plugin_file

        for plugin in raw.get("plugins", []):
            try:
                load_plugin_file(base / plugin)
            except Exception as e:  # noqa: BLE001 - report any plugin failure as a rules problem
                problems.append(f"plugin {plugin}: {type(e).__name__}: {e}")
        for m in metrics:
            cls = registry.get(m.analyzer)
            if cls is None:
                problems.append(
                    f"metric '{m.name}': unknown analyzer '{m.analyzer}' "
                    f"(available: {', '.join(sorted(registry))})"
                )
                continue
            for msg in cls().validate(m.params, rules):
                problems.append(f"metric '{m.name}' ({m.analyzer}): {msg}")

    if problems:
        raise RulesError(path, problems)
    return rules


def hash_paths(paths) -> str:
    """Stable hash over files/folders the grade depends on (tests, reference,
    generators, plagiarism corpus): change any of them and the hash changes."""
    h = hashlib.sha256()
    entries = []
    for root in sorted({Path(p).resolve() for p in paths if p}):
        if root.is_dir():
            for f in sorted(root.rglob("*")):
                rel = f.relative_to(root)
                if f.is_file() and not any(part.startswith((".", "__pycache__")) for part in rel.parts):
                    entries.append((f"{root.name}/{rel.as_posix()}", f))
        elif root.is_file():
            entries.append((root.name, root))
    for name, f in entries:
        h.update(name.encode() + b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()
