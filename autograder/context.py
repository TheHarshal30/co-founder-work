"""Shared state for grading one submission: source, builds, runs, reference
answers. Analyzers run in parallel threads, so everything here is thread-safe
and every expensive step (compiling, running the reference) happens once."""

from __future__ import annotations

import atexit
import hashlib
import random
import shutil
import tempfile
import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import sandbox
from .compiler import BuildResult, compile_c
from .diagnostics import SANITIZER_ENV
from .llm import LLMClient
from .rules import Rules

DEFAULT_SANITIZER_FLAGS = ["-fsanitize=address,undefined"]


class ReferenceError(RuntimeError):
    """The instructor's reference solution failed -- an infrastructure problem,
    never the student's fault."""


class ReferenceSolution:
    """Compiled once per process and shared by every submission in a batch."""

    _instances: Dict[str, "ReferenceSolution"] = {}
    _lock = threading.Lock()

    def __init__(self, rules: Rules):
        assert rules.reference is not None
        self.path = rules.reference
        self.time_limit_s = max(10.0, rules.time_limit_s * 10)
        self._dir = Path(tempfile.mkdtemp(prefix="ag-ref-"))
        atexit.register(shutil.rmtree, self._dir, True)
        src = self._dir / self.path.name
        shutil.copy(self.path, src)
        self.build = compile_c(rules.cc, src, self._dir / "reference", rules.compile_flags, rules.ldflags)
        if not self.build.ok:
            raise ReferenceError(f"reference solution does not compile:\n{self.build.output}")
        self._outputs: Dict[str, str] = {}
        self._out_lock = threading.Lock()

    @classmethod
    def for_rules(cls, rules: Rules) -> "ReferenceSolution":
        if rules.reference is None:
            raise ReferenceError("the rules file names no 'reference' solution, which this analyzer needs")
        key = hashlib.sha256(rules.reference.read_bytes()).hexdigest() + " ".join(rules.compile_flags)
        with cls._lock:
            if key not in cls._instances:
                cls._instances[key] = ReferenceSolution(rules)
            return cls._instances[key]

    def output(self, stdin: str) -> str:
        k = hashlib.sha256(stdin.encode()).hexdigest()
        with self._out_lock:
            if k in self._outputs:
                return self._outputs[k]
        r = sandbox.run([str(self.build.binary)], stdin, time_limit_s=self.time_limit_s)
        if not r.ok:
            raise ReferenceError(f"reference solution {r.describe()} on input {stdin[:80]!r}")
        with self._out_lock:
            self._outputs[k] = r.stdout
        return r.stdout


_GENERATORS: Dict[Path, Callable] = {}
_GEN_LOCK = threading.Lock()


def load_generator(path: Path) -> Callable[[int, random.Random], str]:
    """Load ``generate(size, rng) -> str`` from an instructor's generator file.
    Executed from source (no .pyc written) so the problem folder -- and with it
    the assets hash recorded in the report -- never changes as a side effect."""
    path = path.resolve()
    with _GEN_LOCK:
        if path not in _GENERATORS:
            ns: dict = {"__name__": f"autograder_generator_{path.stem}", "__file__": str(path)}
            exec(compile(path.read_text(), str(path), "exec"), ns)
            fn = ns.get("generate")
            if not callable(fn):
                raise ValueError(f"{path} must define generate(size, rng) -> str")
            _GENERATORS[path] = fn
        return _GENERATORS[path]


def seeded_rng(seed, *parts) -> random.Random:
    # String seeds are hashed with SHA-512 by `random`, independent of
    # PYTHONHASHSEED, so the same (seed, parts) always gives the same stream.
    return random.Random(":".join(str(p) for p in (seed, *parts)))


class Context:
    def __init__(self, submission: Path, rules: Rules, llm: LLMClient, workdir: Path):
        self.submission = Path(submission).resolve()
        self.name = self.submission.name
        self.source = self.submission.read_text(errors="replace")
        self.source_sha256 = hashlib.sha256(self.submission.read_bytes()).hexdigest()
        self.rules = rules
        self.llm = llm
        self.workdir = workdir
        self.src_dir = workdir / "src"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.src_path = self.src_dir / self.name
        shutil.copy(self.submission, self.src_path)
        self.scratch = workdir / "runs"
        self.scratch.mkdir(exist_ok=True)
        self._builds: Dict[str, BuildResult] = {}
        self._build_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._lock = threading.Lock()
        self.first_pass = None  # set by the coordinator

    # -- building ------------------------------------------------------------
    def build(self, key: str, flags: Sequence[str], ldflags: Optional[Sequence[str]] = None) -> BuildResult:
        with self._lock:
            lock = self._build_locks[key]
        with lock:
            if key not in self._builds:
                out = self.workdir / "build" / key / "prog"
                self._builds[key] = compile_c(
                    self.rules.cc, self.src_path, out, list(flags),
                    self.rules.ldflags if ldflags is None else list(ldflags),
                )
            return self._builds[key]

    def main_build(self) -> BuildResult:
        return self.build("main", self.rules.compile_flags)

    def sanitizer_build(self, sanitizer_flags: Optional[Sequence[str]] = None) -> BuildResult:
        san = list(sanitizer_flags or DEFAULT_SANITIZER_FLAGS)
        base = [f for f in self.rules.compile_flags if not f.startswith("-O")]
        flags = base + ["-O1", "-g", "-fno-omit-frame-pointer", *san]
        return self.build("sanitize:" + " ".join(san), flags)

    def debug_build(self) -> BuildResult:
        base = [f for f in self.rules.compile_flags if not f.startswith("-O")]
        return self.build("debug", base + ["-O0", "-g"])

    # -- running -------------------------------------------------------------
    def run(
        self,
        build: BuildResult,
        stdin: str,
        *,
        time_limit_s: Optional[float] = None,
        memory_limit_mb: Optional[int] = -1,
        wall_limit_s: Optional[float] = None,
        prefix: Sequence[str] = (),
        env: Optional[Dict[str, str]] = None,
    ) -> sandbox.RunResult:
        assert build.ok and build.binary is not None
        return sandbox.run(
            [*prefix, str(build.binary)], stdin,
            time_limit_s=time_limit_s or self.rules.time_limit_s,
            memory_limit_mb=self.rules.memory_limit_mb if memory_limit_mb == -1 else memory_limit_mb,
            wall_limit_s=wall_limit_s, env=env, scratch_dir=self.scratch,
        )

    def run_sanitized(self, stdin: str, sanitizer_flags: Optional[Sequence[str]] = None,
                      time_factor: float = 5.0) -> Optional[sandbox.RunResult]:
        """Run under ASan/UBSan; None if the sanitizer build is impossible."""
        b = self.sanitizer_build(sanitizer_flags)
        if not b.ok:
            return None
        # ASan reserves terabytes of virtual memory, so RLIMIT_AS must be off.
        return self.run(b, stdin, time_limit_s=self.rules.time_limit_s * time_factor,
                        memory_limit_mb=None, env=SANITIZER_ENV)

    # -- reference answers ---------------------------------------------------
    def reference(self) -> ReferenceSolution:
        return ReferenceSolution.for_rules(self.rules)

    def expected_output(self, stdin: str) -> str:
        return self.reference().output(stdin)

    def generator(self, rel_path: str) -> Callable[[int, random.Random], str]:
        return load_generator(self.rules.resolve(rel_path))

    def asset_paths(self) -> List[Path]:
        """Every file the grade depends on besides the submission and rules."""
        paths: List[Path] = []
        if self.rules.reference:
            paths.append(self.rules.reference)
        for default in ("edge_cases.json", "gen.py"):
            if (self.rules.base_dir / default).exists():
                paths.append(self.rules.base_dir / default)
        for m in self.rules.metrics:
            for value in m.params.values():
                if isinstance(value, str) and 0 < len(value) < 256:
                    p = self.rules.resolve(value)
                    try:
                        if p.exists():
                            paths.append(p)
                    except OSError:
                        pass
        return paths
