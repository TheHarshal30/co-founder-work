"""Expected outputs: where they may come from, and the optional LLM oracle.

Trust order (the submission is never on this list -- it is what is being graded):

1. outputs the instructor wrote (``tests/*.out``, ``"expected"`` in
   edge_cases.json, ``sample_output`` / ``asset_generation.examples``);
2. the instructor's reference solution;
3. only if the rules opt in (``"oracle": "llm_consensus"``): LLM-written
   programs, accepted only if
   * they pass a static screen (standard headers only; no process, file or
     network calls; no inline assembly),
   * they compile and run under the same sandbox as student code,
   * each one reproduces EVERY instructor example output (at least
     ``min_examples`` of them; the examples are never shown back to the model
     on failure, so they stay an independent check), and
   * at least two *different* programs, written in different styles (brute
     force vs. efficient), agree on every validation input.
   Any disagreement rejects the oracle entirely: no "majority vote" outputs.

If none of these exists, correctness is reported as not performed.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .. import sandbox
from ..compare import outputs_match
from ..compiler import BuildResult, compile_c

ALLOWED_HEADERS = {
    "stdio.h", "stdlib.h", "string.h", "math.h", "limits.h", "stdint.h", "stdbool.h", "ctype.h",
    "inttypes.h", "float.h", "stddef.h", "assert.h",
}
_INCLUDE = re.compile(r"^\s*#\s*include\s*(\S.*)$", re.M)
_FORBIDDEN = [
    (re.compile(r"\b(system|popen|fork|vfork|execl|execlp|execle|execv|execvp|execve|posix_spawn)\s*\("),
     "starts processes"),
    (re.compile(r"\b(fopen|freopen|open|openat|creat|remove|unlink|rename|mkdir|rmdir|chmod|tmpfile)\s*\("),
     "touches the file system"),
    (re.compile(r"\b(socket|connect|bind|listen|accept|kill|ptrace|dlopen|syscall|mmap|mprotect|signal)\s*\("),
     "uses system facilities"),
    (re.compile(r"\b(__asm__|asm)\b"), "contains inline assembly"),
    (re.compile(r"^\s*#\s*(pragma|line)\b", re.M), "contains #pragma/#line"),
    (re.compile(r"__attribute__"), "uses __attribute__"),
]
MAX_SOURCE_BYTES = 20_000


def screen_c_source(code: str) -> List[str]:
    """Static checks before an LLM-written program is ever compiled."""
    problems = []
    if len(code.encode()) > MAX_SOURCE_BYTES:
        problems.append(f"program is longer than {MAX_SOURCE_BYTES} bytes")
    if not re.search(r"\bmain\s*\(", code):
        problems.append("no main() function")
    for m in _INCLUDE.finditer(code):
        target = m.group(1).strip()
        header = target[1:-1] if target.startswith("<") and target.endswith(">") else None
        if header not in ALLOWED_HEADERS:
            problems.append(f"#include {target[:40]} is not an allowed standard header")
    for pattern, why in _FORBIDDEN:
        if pattern.search(code):
            problems.append(f"program {why}")
    return problems


@dataclass
class Candidate:
    label: str
    code: str
    build: Optional[BuildResult] = None
    problems: List[str] = field(default_factory=list)
    outputs: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


class CandidateRunner:
    """Compiles and runs candidate programs in a private temporary folder,
    with the rules' compiler, flags and (generous) limits."""

    def __init__(self, cc: str, flags: Sequence[str], ldflags: Sequence[str], time_limit_s: float,
                 memory_limit_mb: Optional[int]):
        self.cc, self.flags, self.ldflags = cc, list(flags), list(ldflags)
        self.time_limit_s = max(5.0, time_limit_s * 10)
        self.memory_limit_mb = memory_limit_mb
        self.dir = Path(tempfile.mkdtemp(prefix="ag-oracle-"))

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def compile(self, cand: Candidate) -> None:
        problems = screen_c_source(cand.code)
        if problems:
            cand.problems += problems
            return
        d = self.dir / cand.label
        d.mkdir(parents=True, exist_ok=True)
        src = d / "candidate.c"
        src.write_text(cand.code)
        cand.build = compile_c(self.cc, src, d / "candidate", self.flags, self.ldflags)
        if not cand.build.ok:
            cand.problems.append("does not compile: " + cand.build.output[:300])

    def run(self, cand: Candidate, stdin: str) -> Optional[str]:
        assert cand.build is not None and cand.build.binary is not None
        r = sandbox.run([str(cand.build.binary)], stdin, time_limit_s=self.time_limit_s,
                        memory_limit_mb=self.memory_limit_mb, scratch_dir=self.dir)
        return r.stdout if r.ok else None


def compile_problems(runner: CandidateRunner, code: str, label: str) -> List[str]:
    """Used as the retry validator while generating: screen + compile only.
    (Example outputs are deliberately NOT fed back; see the module docstring.)"""
    cand = Candidate(label, code)
    runner.compile(cand)
    return cand.problems


@dataclass
class OracleVerdict:
    accepted: Optional[Candidate]
    candidates: List[Candidate]
    expected: Dict[str, str]  # validation input -> agreed output
    reason: str


def select_oracle(runner: CandidateRunner, codes: List[Tuple[str, str]], examples: List[Tuple[str, str]],
                  validation_inputs: List[str], compare: str = "trim", min_examples: int = 2,
                  min_agreeing: int = 2) -> OracleVerdict:
    """Apply the acceptance rules in the module docstring to candidate programs."""
    cands = []
    seen_code = set()
    for label, code in codes:
        c = Candidate(label, code)
        if code.strip() in seen_code:
            c.problems.append("identical to an earlier candidate (not an independent version)")
        seen_code.add(code.strip())
        cands.append(c)
    if len(examples) < min_examples:
        for c in cands:
            c.problems.append("not enough instructor examples to validate against")
        return OracleVerdict(None, cands, {}, f"only {len(examples)} instructor example(s) with outputs; "
                                              f"an LLM oracle needs at least {min_examples}")
    for c in cands:
        if c.ok:
            runner.compile(c)
        if not c.ok:
            continue
        for i, (stdin, want) in enumerate(examples, 1):
            got = runner.run(c, stdin)
            if got is None or not outputs_match(want, got, compare):
                c.problems.append(f"wrong or failed on instructor example {i}")
                break
    good = [c for c in cands if c.ok]
    if len(good) < min_agreeing:
        return OracleVerdict(None, cands, {}, f"{len(good)} candidate(s) reproduced every instructor example; "
                                              f"{min_agreeing} independent ones are required")
    for stdin in validation_inputs:
        outs = []
        for c in good:
            out = runner.run(c, stdin)
            if out is None:
                c.problems.append(f"crashed or timed out on a valid input {stdin[:60]!r}")
                return OracleVerdict(None, cands, {}, f"candidate {c.label} failed on a valid input")
            c.outputs[stdin] = out
            outs.append(out)
        if not all(outputs_match(outs[0], o, compare) for o in outs[1:]):
            return OracleVerdict(None, cands, {}, f"candidates disagree on input {stdin[:60]!r}; "
                                                  "no trustworthy expected output")
    # The efficient candidate (if it validated) is the oracle used at grade time.
    chosen = next((c for c in good if c.label == "efficient"), good[0])
    expected = {s: chosen.outputs[s] for s in validation_inputs}
    return OracleVerdict(chosen, cands, expected,
                         f"{len(good)} independent programs reproduced {len(examples)} instructor examples "
                         f"and agreed on {len(validation_inputs)} validation inputs")
