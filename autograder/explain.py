"""Turn a failing run into a 'WHY' (what went wrong on this input) and a 'FIX'
(what to change) that a student can act on.

The default explainer is rule-based and therefore deterministic: it looks at the
verdict, the sanitizer diagnostic and the shape of the wrong answer. The FIX
text is deliberately input-independent so that the same mistake seen on three
inputs produces one suggestion, not three. An optional LLM explainer can add a
problem-aware explanation; its answers are cached like every other LLM call,
so feedback stays reproducible too.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional, Tuple

from .sandbox import OUTPUT_LIMIT_MB, RunResult

if TYPE_CHECKING:  # pragma: no cover
    from .analyzers.base import Counterexample
    from .context import Context

_SIGNALS = {
    "SIGSEGV": ("Segmentation fault: the program read or wrote memory it does not own.",
                "Check array indices against the array size and make sure every pointer is "
                "initialised (and not NULL) before use."),
    "SIGBUS": ("Bus error: an invalid memory access (misaligned or unmapped address).",
               "Check pointer arithmetic and casts."),
    "SIGFPE": ("Arithmetic exception: most likely an integer division or modulo by zero.",
               "Guard every division/modulo against a zero divisor."),
    "SIGABRT": ("The program aborted: a failed assert(), or the C library detected a corrupted "
                "heap (double free, writing past the end of a malloc'd block).",
                "Check every malloc'd buffer is large enough and freed exactly once."),
    "SIGILL": ("Illegal instruction: usually undefined behaviour the compiler turned into a trap.",
               "Look for undefined behaviour such as a missing return value."),
}

FIX_OVERFLOW = ("Use a wider type (long long) for values that can exceed the int range, or check "
                "the operands before the operation.")


def _as_int(s: str) -> Optional[int]:
    parts = s.split()
    if len(parts) != 1:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


GENERIC_FIX = "Trace your program by hand on this input."
LARGE_INPUT_BYTES = 10_000


def explain(verdict: str, expected: str, got: str, run: Optional[RunResult],
            diagnostic: Optional[str] = None, time_limit_s: float = 1.0,
            input_len: int = 0) -> Tuple[str, str]:
    """Return (why, fix)."""
    if diagnostic and "signed integer overflow" in diagnostic:
        detail = diagnostic.split("runtime error:", 1)[-1].strip()
        return (f"Integer overflow: {detail}. The result does not fit in a 32-bit int, which is "
                "undefined behaviour in C.", FIX_OVERFLOW)
    if verdict == "timeout" and input_len > LARGE_INPUT_BYTES:
        return (f"The program did not finish within {time_limit_s:g}s on this large input "
                f"({input_len:,} bytes), although it handles small inputs.",
                "The algorithm is too slow for the largest allowed input; reduce its time "
                "complexity (see the complexity metric for the measured growth rate).")
    if verdict == "timeout":
        return (f"The program did not finish within {time_limit_s:g}s on this input.",
                "Look for a loop whose exit condition is never met (e.g. `while (scanf(...) != 0)` "
                "never stops, because scanf returns EOF = -1 at end of input) or an algorithm that "
                "is too slow for the input size.")
    if verdict == "output_limit":
        return (f"The program printed more than {OUTPUT_LIMIT_MB} MB of output.",
                "Make sure every output loop terminates.")
    if verdict == "memory_limit":
        return ("The program used more memory than allowed for this problem.",
                "Avoid allocating memory proportional to a value you could process on the fly.")
    if verdict == "runtime_error":
        why, fix = _SIGNALS.get(run.signal_name if run else "", ("The program crashed.", "Run it "
                                "under a debugger or with -fsanitize=address to find the crash."))
        return (why + (f" Sanitizer: {diagnostic}" if diagnostic else ""), fix)
    if verdict == "nonzero_exit":
        code = run.exit_code if run else "?"
        return (f"The output is right but main() returned {code}.",
                "Return 0 from main() when the program succeeds.")
    if diagnostic:
        return (f"Memory/undefined-behaviour error on this input: {diagnostic}",
                "Fix the memory error reported by the sanitizer; the output only looked right by luck.")
    if verdict == "presentation_error":
        return ("The values are right but the spacing or line breaks differ.",
                "Print exactly the required format: no extra spaces, labels or blank lines.")

    # wrong answer
    if not got.strip():
        return ("The program printed nothing for this input.",
                "Make sure every input path reaches the printf of the answer.")
    e, g = _as_int(expected), _as_int(got)
    if e is not None and g is not None:
        diff = e - g
        if diff % (1 << 32) == 0:
            return (f"Expected {e} but got {g}: they differ by exactly a multiple of 2^32, so a "
                    "32-bit int wrapped around (overflow).", FIX_OVERFLOW)
        if g == 0 and e < 0:
            return (f"You printed 0 but the answer is {e} (negative). A running maximum or best value "
                    "initialised to 0 can never become negative.",
                    "Initialise the best value from the first element (or LLONG_MIN), not from 0.")
        if abs(diff) == 1:
            return (f"Expected {e} but got {g}: off by one.",
                    "Check loop bounds (< vs <=) and where counting starts.")
        if e == -g:
            return f"Expected {e} but got {g}: the sign is wrong.", "Check the sign of each term."
        return f"Expected {e} but got {g}.", GENERIC_FIX
    exp_tokens, got_tokens = expected.split(), got.split()
    if exp_tokens and all(t in got_tokens for t in exp_tokens) and len(got_tokens) > len(exp_tokens):
        return (f"The right answer appears in your output, but surrounded by extra text "
                f"({got.strip()[:60]!r}).",
                "Print only what the problem asks for -- no prompts like 'Enter a number' and no labels.")
    exp_lines, got_lines = expected.rstrip("\n").split("\n"), got.rstrip("\n").split("\n")
    if len(exp_lines) != len(got_lines):
        return (f"Expected {len(exp_lines)} line(s) of output but got {len(got_lines)}.",
                "Print exactly one line per required answer.")
    for i, (a, b) in enumerate(zip(exp_lines, got_lines), 1):
        if a.rstrip() != b.rstrip():
            return (f"Line {i}: expected {a.strip()[:80]!r}, got {b.strip()[:80]!r}.",
                    GENERIC_FIX)
    return "Output differs from the expected output.", GENERIC_FIX


_EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {"explanation": {"type": "string"}, "fix_hint": {"type": "string"}},
    "required": ["explanation", "fix_hint"],
}


def llm_explain(ctx: "Context", ce: "Counterexample", params: dict) -> Optional[Tuple[str, str]]:
    """Ask the LLM for a problem-aware (why, fix) for one counterexample.
    Returns None (caller keeps the rule-based text) if no LLM is reachable."""
    from .llm import LLMUnavailable

    numbered = "\n".join(f"{i:3d}| {l}" for i, l in enumerate(ctx.source.splitlines(), 1))
    statement = ctx.rules.statement_text() or f"Problem: {ctx.rules.problem}"
    messages = [
        {"role": "system", "content": (
            "You are a teaching assistant. A student's C program gave a wrong result on one "
            "input. In at most 2 sentences explain WHY the code fails on this input, citing line "
            "numbers; then give a 1-sentence fix hint. Do not write code. Reply as JSON with keys "
            "explanation and fix_hint.")},
        {"role": "user", "content": (
            f"{statement}\n\nStudent code:\n{numbered}\n\nInput:\n{ce.input}\n"
            f"Expected output:\n{ce.expected}\nStudent output:\n{ce.got}\n"
            f"Verdict: {ce.verdict}\nAutomatic analysis: {ce.why}")},
    ]
    try:
        ans = ctx.llm.chat_json(
            provider=params.get("llm_provider", "ollama"),
            model=params.get("llm_model", "qwen2.5:7b"),
            messages=messages, temperature=0.0, seed=int(params.get("seed", 42)),
            schema=_EXPLAIN_SCHEMA,
        )
    except LLMUnavailable:
        return None
    why = re.sub(r"\s+", " ", str(ans.data.get("explanation", ""))).strip()
    fix = re.sub(r"\s+", " ", str(ans.data.get("fix_hint", ""))).strip()
    return (why, fix or ce.fix) if why else None
