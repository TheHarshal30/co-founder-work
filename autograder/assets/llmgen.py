"""Ask the LLM for candidate assets, with validation and bounded retries.

Every answer goes through ``LLMClient.chat_json`` (temperature 0, fixed seed,
content-addressed cache), so a second preparation with the same inputs replays
the same answers, offline too. An answer that fails its validator is retried at
most ``max_retries`` times with the validator's complaints appended; after that
the step is reported as a *generation failure* (never as a student failure).

What the model sees is a *problem brief* built from the instructor's material
only: the problem title, the statement (if any), limits, the sample and any
example outputs the instructor chose to publish. The submission is never shown
to the model when it writes tests, oracles or solutions -- generated tests must
not inherit the student's misreading of the problem. The one exception is the
input FORMAT: when the instructor gave no input at all, the submission's
input-reading statements are offered as a hint, and the spec is reported as
inferred that way.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..llm import LLMClient, LLMUnavailable
from .inputspec import check_spec

PROMPT_VERSION = "asset-prompts-v2"

Check = Callable[[Dict[str, Any]], Tuple[Any, List[str]]]


@dataclass
class Settings:
    provider: str = "ollama"
    model: str = "qwen2.5:7b"
    seed: int = 42
    max_retries: int = 3
    cache_salt: Optional[str] = None  # the model digest, when known


class Generator:
    def __init__(self, llm: LLMClient, settings: Settings):
        self.llm = llm
        self.s = settings
        self.events: List[Dict[str, Any]] = []
        # why the last failed ask() failed: "llm_error" (nothing usable came back
        # -> asset unavailable) or "rejected" (answers failed validation)
        self.last_outcome = ""
        self.last_error = ""

    def log(self, step: str, attempt: int, outcome: str, detail: str = "", key: Optional[str] = None) -> None:
        self.events.append({"step": step, "attempt": attempt, "outcome": outcome,
                            "detail": detail[:2000], "llm_cache_key": key})

    def ask(self, step: str, messages: List[Dict[str, str]], schema: dict, check: Check,
            seed_offset: int = 0) -> Any:
        """Validated value, or None after ``max_retries`` failed attempts."""
        msgs = list(messages)
        self.last_outcome, self.last_error = "llm_error", "no attempt made"
        for attempt in range(1, self.s.max_retries + 1):
            try:
                ans = self.llm.chat_json(provider=self.s.provider, model=self.s.model, messages=msgs,
                                         temperature=0.0, seed=self.s.seed + seed_offset, schema=schema,
                                         cache_salt=self.s.cache_salt)
            except LLMUnavailable as e:
                self.log(step, attempt, "llm_error", str(e))
                self.last_outcome, self.last_error = "llm_error", f"LLM unavailable: {e}"
                if "JSON" not in str(e):
                    return None  # unreachable / offline / misconfigured: retrying cannot help
                continue
            try:
                value, problems = check(ans.data)
            except Exception as e:  # noqa: BLE001 - a malformed answer is a rejection, not a crash
                value, problems = None, [f"{type(e).__name__}: {e}"]
            if not problems:
                self.log(step, attempt, "accepted", "", ans.cache_key)
                return value
            self.log(step, attempt, "rejected", "; ".join(problems), ans.cache_key)
            self.last_outcome = "rejected"
            self.last_error = f"{attempt} answer(s) rejected; last: " + "; ".join(problems)[:300]
            msgs = msgs + [
                {"role": "assistant", "content": ans.raw},
                {"role": "user", "content": "A validator rejected that answer:\n- " + "\n- ".join(problems[:8])
                                            + "\nReply again with corrected JSON only."},
            ]
        return None

    def failed_status(self) -> str:
        return "rejected" if self.last_outcome == "rejected" else "unavailable"


# -- the problem brief ----------------------------------------------------------------
def humanize_title(problem: str) -> str:
    return re.sub(r"[_\-]+", " ", str(problem)).strip() or "unknown problem"


def problem_brief(title: str, statement: str, shown_examples: List[Tuple[str, str]],
                  sample_inputs: List[str], limits: Dict[str, Any], hints: Dict[str, Any]) -> str:
    """Everything the instructor published about the problem, as one text.
    Held-out examples are deliberately not part of it: they validate the
    oracle independently."""
    parts = [f"Problem title: {title}"]
    if statement.strip():
        parts.append("Statement:\n" + statement.strip())
    else:
        parts.append("Statement: (none given; infer the task from the title, the rules and the examples)")
    lim = []
    if limits.get("time_limit_s"):
        lim.append(f"time limit {limits['time_limit_s']:g} s")
    if limits.get("memory_limit_mb"):
        lim.append(f"memory limit {limits['memory_limit_mb']} MB")
    if limits.get("required_complexity"):
        lim.append(f"required time complexity {limits['required_complexity']}")
    if lim:
        parts.append("Limits: " + ", ".join(lim) + ".")
    if hints.get("case_families"):
        parts.append("Corner cases the instructor wants tested: " + ", ".join(hints["case_families"]))
    if hints.get("compare"):
        parts.append(f"Output comparison: {hints['compare']}")
    for i, (inp, out) in enumerate(shown_examples, 1):
        parts.append(f"Example {i} input:\n{inp.rstrip()}\nExample {i} expected output:\n{out.rstrip()}")
    shown = {i for i, _ in shown_examples}
    for i, s in enumerate([s for s in sample_inputs if s not in shown], 1):
        parts.append(f"Sample input {i} (expected output not given):\n{s.rstrip()}")
    return "\n\n".join(parts)


_READ_CALL = re.compile(r"\b(scanf|fscanf|sscanf|getchar|fgets|getline|getc|fgetc|fread|gets)\s*\(")


def input_format_hint(source: str, max_lines: int = 15) -> str:
    """The statements a C program uses to read stdin (a format hint, nothing more)."""
    lines = [l.strip() for l in source.splitlines() if _READ_CALL.search(l)]
    return "\n".join(lines[:max_lines])


# -- input specification ----------------------------------------------------------
SPEC_SYSTEM = """You describe the INPUT FORMAT of a programming problem as JSON, so a tool can generate and check test inputs.
Reply with JSON only: {"spec": SPEC}. SPEC = {"version": 1, "lines": [LINE, ...], "unsupported_constraints": [strings]}.
LINE = {"tokens": [TOKEN, ...]} with optional "repeat": N or the name of an earlier int (the line occurs that many times).
TOKEN = {"name": identifier, "type": "int" | "float" | "word", ...}:
- int: "min", "max" (integers, or the name of an earlier int); optional "count": N or earlier int name (an array of that many values on this line); optional "distinct": true, "sorted": "asc" | "desc"; optional "size": true on the ONE int that is the main input size (e.g. n).
- float: "min", "max" numbers, "decimals".
- word: "charset" (lower, upper, alpha, digits, alnum), "min_len", "max_len".
Use exactly the bounds stated in the problem. When no bounds are stated, use the natural C type range (e.g. a 32-bit int: -2147483648..2147483647) and keep array sizes at most 200000.
Every sample input shown must match your spec. If a constraint cannot be expressed (e.g. "the graph is connected"), list it in "unsupported_constraints" instead of ignoring it.
Example for "first line n (1<=n<=100), second line n integers |a_i|<=1000":
{"spec": {"version": 1, "lines": [{"tokens": [{"name": "n", "type": "int", "min": 1, "max": 100, "size": true}]}, {"tokens": [{"name": "a", "type": "int", "min": -1000, "max": 1000, "count": "n"}]}], "unsupported_constraints": []}}"""

SPEC_SCHEMA = {"type": "object", "properties": {"spec": {"type": "object"}}, "required": ["spec"]}


def propose_input_spec(gen: Generator, brief: str, instructor_inputs: List[str],
                       format_hint: str = "") -> Optional[dict]:
    """An input spec that is well-formed AND accepts every instructor-written
    input (the independent check: the instructor's own inputs must parse)."""
    from .inputspec import InputSpec

    def check(data):
        spec = data.get("spec")
        problems = check_spec(spec)
        if problems:
            return None, problems
        parsed = InputSpec(spec)
        for i, s in enumerate(instructor_inputs, 1):
            errs = parsed.validate(s)
            if errs:
                problems.append(f"instructor input {i} does not match the spec: {errs[0]}")
        return spec, problems

    user = brief
    if format_hint:
        user += ("\n\nNo instructor input is available. A student's program reads its input with these "
                 "statements (they may be wrong; use them only as a hint about the layout):\n" + format_hint)
    messages = [{"role": "system", "content": SPEC_SYSTEM}, {"role": "user", "content": user}]
    return gen.ask("input_spec", messages, SPEC_SCHEMA, check)


# -- corner-case inputs -----------------------------------------------------------
CASES_SYSTEM = """You design CORNER-CASE TEST INPUTS for a programming problem. You only write inputs, never outputs or code.
Every input must follow the given input format specification exactly (same lines, counts and bounds) and be small (a few lines).
Reply with JSON only: {"families": [{"name": snake_case_name, "why": one sentence, "inputs": [input strings using \\n for line breaks]}]}.
Give up to 4 inputs per family."""

CASES_SCHEMA = {
    "type": "object",
    "properties": {"families": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "why": {"type": "string"},
        "inputs": {"type": "array", "items": {"type": "string"}}}, "required": ["name", "inputs"]}}},
    "required": ["families"],
}


def propose_case_families(gen: Generator, brief: str, spec: dict,
                          wanted: Optional[List[str]], max_families: int) -> Optional[List[dict]]:
    if wanted:
        ask = (f"Write exactly these families (use these names): {', '.join(wanted)}. "
               "Interpret each name as the kind of corner case it describes.")
    else:
        ask = (f"Choose up to {max_families} families that are likely to break wrong solutions "
               "(extremes of every bound, sign changes, overflow, smallest/largest sizes, ties).")

    def check(data):
        fams = data.get("families")
        if not isinstance(fams, list) or not fams:
            return None, ["'families' must be a non-empty list"]
        out, problems = [], []
        for f in fams:
            if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not isinstance(f.get("inputs"), list):
                problems.append("each family needs a string 'name' and a list 'inputs'")
                continue
            out.append({"name": f["name"], "why": str(f.get("why", ""))[:300],
                        "inputs": [str(i) for i in f["inputs"][:4]]})
        if wanted:
            missing = [w for w in wanted if w not in {f["name"] for f in out}]
            if missing:
                problems.append(f"missing families: {missing}")
        return out[: max(max_families, len(wanted or []))], problems

    messages = [{"role": "system", "content": CASES_SYSTEM},
                {"role": "user", "content": f"{brief}\n\nInput format specification:\n{json.dumps(spec)}\n\n{ask}"}]
    return gen.ask("case_families", messages, CASES_SCHEMA, check)


# -- C programs (oracle candidates, known solutions) ------------------------------
CODE_SCHEMA = {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}

ORACLE_STYLES = [
    "Write the simplest solution that is obviously correct, even if it is slow (brute force is fine).",
    "Write an efficient solution that meets the problem's limits.",
    "Write a careful solution that handles every edge case explicitly (extreme values, overflow, empty or minimal input).",
]


def propose_program(gen: Generator, step: str, brief: str, style: str,
                    check_code: Callable[[str], List[str]], seed_offset: int) -> Optional[str]:
    system = ("You write a complete C11 program that reads the input from stdin and prints exactly the required "
              "output to stdout, nothing else (no prompts). Use only the standard C library. When the statement "
              "does not say otherwise, compute the mathematically exact answer (use long long where a sum or "
              'product may overflow int). Reply with JSON only: {"code": "<the full program>"}.')

    def check(data):
        code = data.get("code")
        if not isinstance(code, str) or not code.strip():
            return None, ["'code' must be a non-empty string"]
        return code, check_code(code)

    # the style comes first: it is what makes the candidates independent versions
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"{style}\n\n{brief}"}]
    return gen.ask(step, messages, CODE_SCHEMA, check, seed_offset=seed_offset)
