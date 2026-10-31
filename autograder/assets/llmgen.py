"""Ask the LLM for candidate assets, with validation and bounded retries.

Every answer goes through ``LLMClient.chat_json`` (temperature 0, fixed seed,
content-addressed cache), so a second preparation with the same inputs replays
the same answers, offline too. An answer that fails its validator is retried at
most ``max_retries`` times with the validator's complaints appended; after that
the step is reported as a *generation failure* (never as a student failure).

The submission under evaluation is never shown to the model: generated tests
must not inherit the student's misreading of the problem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..llm import LLMClient, LLMUnavailable
from .inputspec import check_spec

PROMPT_VERSION = "asset-prompts-v1"

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


# -- input specification ----------------------------------------------------------
SPEC_SYSTEM = """You describe the INPUT FORMAT of a programming problem as JSON, so a tool can generate and check test inputs.
Reply with JSON only: {"spec": SPEC}. SPEC = {"version": 1, "lines": [LINE, ...], "unsupported_constraints": [strings]}.
LINE = {"tokens": [TOKEN, ...]} with optional "repeat": N or the name of an earlier int (the line occurs that many times).
TOKEN = {"name": identifier, "type": "int" | "float" | "word", ...}:
- int: "min", "max" (integers, or the name of an earlier int); optional "count": N or earlier int name (an array of that many values on this line); optional "distinct": true, "sorted": "asc" | "desc"; optional "size": true on the ONE int that is the main input size (e.g. n).
- float: "min", "max" numbers, "decimals".
- word: "charset" (lower, upper, alpha, digits, alnum), "min_len", "max_len".
Use exactly the bounds stated in the problem. If a constraint cannot be expressed (e.g. "the graph is connected"), list it in "unsupported_constraints" instead of ignoring it.
Example for "first line n (1<=n<=100), second line n integers |a_i|<=1000":
{"spec": {"version": 1, "lines": [{"tokens": [{"name": "n", "type": "int", "min": 1, "max": 100, "size": true}]}, {"tokens": [{"name": "a", "type": "int", "min": -1000, "max": 1000, "count": "n"}]}], "unsupported_constraints": []}}"""

SPEC_SCHEMA = {"type": "object", "properties": {"spec": {"type": "object"}}, "required": ["spec"]}


def problem_text(statement: str, sample_inputs: List[str]) -> str:
    text = statement.strip() or "(no statement)"
    for i, s in enumerate(sample_inputs, 1):
        text += f"\n\nInstructor sample input {i}:\n{s}"
    return text


def propose_input_spec(gen: Generator, statement: str, instructor_inputs: List[str]) -> Optional[dict]:
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
                problems.append(f"instructor sample input {i} does not match the spec: {errs[0]}")
        return spec, problems

    messages = [{"role": "system", "content": SPEC_SYSTEM},
                {"role": "user", "content": problem_text(statement, instructor_inputs)}]
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


def propose_case_families(gen: Generator, statement: str, spec: dict, instructor_inputs: List[str],
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
                {"role": "user", "content": f"{problem_text(statement, instructor_inputs)}\n\n"
                                            f"Input format specification:\n{json.dumps(spec)}\n\n{ask}"}]
    return gen.ask("case_families", messages, CASES_SCHEMA, check)


# -- C programs (oracle candidates, known solutions) ------------------------------
CODE_SCHEMA = {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}

ORACLE_STYLES = [
    "Write the simplest solution that is obviously correct, even if it is slow (brute force is fine).",
    "Write an efficient solution that meets the problem's limits.",
    "Write a careful solution that handles every edge case explicitly (extreme values, overflow, empty or minimal input).",
]


def propose_program(gen: Generator, step: str, statement: str, instructor_inputs: List[str], style: str,
                    check_code: Callable[[str], List[str]], seed_offset: int) -> Optional[str]:
    system = ("You write a complete C11 program that reads the input from stdin and prints exactly the required "
              "output to stdout, nothing else (no prompts). Use only the standard C library. "
              'Reply with JSON only: {"code": "<the full program>"}.')

    def check(data):
        code = data.get("code")
        if not isinstance(code, str) or not code.strip():
            return None, ["'code' must be a non-empty string"]
        return code, check_code(code)

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"{style}\n\n{problem_text(statement, instructor_inputs)}"}]
    return gen.ask(step, messages, CODE_SCHEMA, check, seed_offset=seed_offset)
