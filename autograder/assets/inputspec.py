"""Input specifications: a small declarative description of a valid test input.

A spec is *data*, never code. It is either written by the instructor
(``asset_generation.input_spec`` in the rules) or proposed by an LLM from the
problem statement and then checked here. The same spec is used to

* validate every generated input (wrong token count, out-of-range value,
  unsorted array, extra lines ... -> rejected with a reason),
* generate inputs deterministically from a seeded RNG (the generated ``gen.py``
  is a fixed template that interprets the spec; no model-written code runs),
* derive boundary cases (minimum size, all-minimum values, ...).

    {"version": 1,
     "lines": [
       {"tokens": [{"name": "n", "type": "int", "min": 1, "max": 200000, "size": true}]},
       {"tokens": [{"name": "a", "type": "int", "min": -1000000000, "max": 1000000000, "count": "n"}]}
     ],
     "unsupported_constraints": []}

Token fields: ``type`` int | float | word; ``min``/``max`` (a number, or for int
the name of an earlier int); ``count`` (array of that many values on the line;
a number or an earlier int); ``size`` (this int is the generator's ``size``);
``distinct`` / ``sorted`` ("asc" | "desc") for int arrays; ``decimals`` for
float; ``charset`` / ``min_len`` / ``max_len`` for word. A line may ``repeat``
(a number or an earlier int). A constraint the format cannot express must be
listed in ``unsupported_constraints``, which makes the spec unusable rather
than silently wrong.
"""

from __future__ import annotations

import random
import re
import string
from typing import Any, Dict, List, Optional, Tuple

SPEC_VERSION = 1
TYPES = ("int", "float", "word")
CHARSETS = {
    "lower": string.ascii_lowercase, "upper": string.ascii_uppercase,
    "alpha": string.ascii_letters, "digits": string.digits,
    "alnum": string.ascii_letters + string.digits,
}
MAX_ABS = 10**18
MAX_COUNT = 10**7
SMALL_SIZE = 12  # small inputs use small values, so a failing case is readable
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


class SpecError(ValueError):
    pass


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_spec(spec: Any) -> List[str]:
    """Problems with ``spec`` ([] = usable). Strict on purpose: a spec that is
    accepted must be one the generator and the validator both honour."""
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    problems: List[str] = []
    if spec.get("version", SPEC_VERSION) != SPEC_VERSION:
        problems.append(f"'version' must be {SPEC_VERSION}")
    unsupported = spec.get("unsupported_constraints", [])
    if not isinstance(unsupported, list):
        problems.append("'unsupported_constraints' must be a list")
    elif unsupported:
        problems.append("the input has constraints this format cannot express: "
                        + "; ".join(str(u) for u in unsupported)[:300])
    lines = spec.get("lines")
    if not isinstance(lines, list) or not lines or len(lines) > 50:
        return problems + ["'lines' must be a non-empty list of at most 50 lines"]

    ints: Dict[str, bool] = {}  # earlier int scalars outside repeated lines (referable)
    names = set()
    sizes = 0

    def ref_ok(v, where) -> None:
        if isinstance(v, str):
            if v not in ints:
                problems.append(f"{where}: '{v}' is not an earlier integer outside a repeated line")
        elif not (isinstance(v, int) and not isinstance(v, bool)):
            problems.append(f"{where}: must be an integer or the name of an earlier integer")

    for li, line in enumerate(lines, 1):
        if not isinstance(line, dict) or not isinstance(line.get("tokens"), list) or not line["tokens"]:
            problems.append(f"line {li}: needs a non-empty 'tokens' list")
            continue
        if len(line["tokens"]) > 20:
            problems.append(f"line {li}: at most 20 tokens")
        repeated = "repeat" in line
        if repeated:
            ref_ok(line["repeat"], f"line {li} repeat")
            if isinstance(line["repeat"], int) and not 0 <= line["repeat"] <= MAX_COUNT:
                problems.append(f"line {li} repeat: out of range")
        for ti, tok in enumerate(line["tokens"], 1):
            where = f"line {li} token {ti}"
            if not isinstance(tok, dict):
                problems.append(f"{where}: must be an object")
                continue
            name, typ = tok.get("name"), tok.get("type")
            if not isinstance(name, str) or not _NAME.match(name):
                problems.append(f"{where}: 'name' must be an identifier")
                continue
            where = f"token '{name}'"
            if name in names:
                problems.append(f"{where}: duplicate name")
            names.add(name)
            if typ not in TYPES:
                problems.append(f"{where}: 'type' must be one of {TYPES}")
                continue
            is_array = "count" in tok
            if is_array:
                ref_ok(tok["count"], f"{where} count")
                if isinstance(tok["count"], int) and not 0 <= tok["count"] <= MAX_COUNT:
                    problems.append(f"{where} count: out of range")
            if typ == "int":
                for k in ("min", "max"):
                    v = tok.get(k)
                    if isinstance(v, str):
                        ref_ok(v, f"{where} {k}")
                    elif not (isinstance(v, int) and not isinstance(v, bool)) or abs(v) > MAX_ABS:
                        problems.append(f"{where}: '{k}' must be an integer with |{k}| <= 10^18 "
                                        f"or an earlier integer's name")
                lo, hi = tok.get("min"), tok.get("max")
                if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
                    problems.append(f"{where}: min > max")
            elif typ == "float":
                lo, hi = tok.get("min"), tok.get("max")
                if not (_is_num(lo) and _is_num(hi)) or lo > hi or max(abs(lo), abs(hi)) > MAX_ABS:
                    problems.append(f"{where}: float needs numeric min <= max")
                d = tok.get("decimals", 2)
                if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 9:
                    problems.append(f"{where}: 'decimals' must be 0..9")
            else:
                if tok.get("charset", "lower") not in CHARSETS:
                    problems.append(f"{where}: 'charset' must be one of {sorted(CHARSETS)}")
                a, b = tok.get("min_len", 1), tok.get("max_len", 10)
                if not (isinstance(a, int) and isinstance(b, int)) or not 1 <= a <= b <= 10000:
                    problems.append(f"{where}: need 1 <= min_len <= max_len <= 10000")
            if tok.get("distinct") or tok.get("sorted"):
                if not (is_array and typ == "int"):
                    problems.append(f"{where}: 'distinct'/'sorted' only apply to int arrays")
                if tok.get("sorted") not in (None, False, "asc", "desc"):
                    problems.append(f"{where}: 'sorted' must be \"asc\" or \"desc\"")
            if tok.get("size"):
                sizes += 1
                if typ != "int" or is_array or repeated:
                    problems.append(f"{where}: the size token must be a single int outside repeated lines")
                elif not (isinstance(tok.get("min"), int) and isinstance(tok.get("max"), int)):
                    problems.append(f"{where}: the size token needs numeric min and max")
                elif tok["min"] < 0:
                    problems.append(f"{where}: the size token must be >= 0")
            if typ == "int" and not is_array and not repeated:
                ints[name] = True
    if sizes > 1:
        problems.append("at most one token may be marked 'size'")
    return problems


class InputSpec:
    def __init__(self, spec: Dict[str, Any]):
        problems = check_spec(spec)
        if problems:
            raise SpecError("; ".join(problems))
        self.spec = spec
        self.lines: List[Dict[str, Any]] = spec["lines"]
        self.size_token: Optional[Dict[str, Any]] = next(
            (t for l in self.lines for t in l["tokens"] if t.get("size")), None)
        # ints that drive a count/repeat: kept near ``size`` so inputs stay small
        self._length_vars = {t.get("count") for l in self.lines for t in l["tokens"]} | \
                            {l.get("repeat") for l in self.lines}

    # -- facts ---------------------------------------------------------------
    def size_range(self) -> Optional[Tuple[int, int]]:
        t = self.size_token
        return (t["min"], t["max"]) if t else None

    def supports_size(self, size: int) -> bool:
        r = self.size_range()
        return r is None or r[0] <= size <= r[1]

    # -- validation ------------------------------------------------------------
    def validate(self, text: str) -> List[str]:
        """Problems with ``text`` as an input ([] = valid)."""
        if not isinstance(text, str) or not text.strip():
            return ["input is empty"]
        rows = text.replace("\r\n", "\n").split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        env: Dict[str, int] = {}
        idx = 0
        for li, line in enumerate(self.lines, 1):
            reps = self._resolve(line.get("repeat", 1), env)
            if reps is None or not 0 <= reps <= MAX_COUNT:
                return [f"line spec {li}: repeat count not available"]
            for _ in range(reps):
                if idx >= len(rows):
                    return [f"input ends early: expected a line for line spec {li}"]
                parts = rows[idx].split()
                idx += 1
                pos = 0
                for tok in line["tokens"]:
                    n = self._resolve(tok["count"], env) if "count" in tok else 1
                    if n is None or not 0 <= n <= MAX_COUNT:
                        return [f"line {idx}: count of '{tok['name']}' not available"]
                    values = parts[pos:pos + n]
                    pos += n
                    if len(values) < n:
                        return [f"line {idx}: expected {n} value(s) for '{tok['name']}', got {len(values)}"]
                    err = self._check_values(tok, values, env)
                    if err:
                        return [f"line {idx}: {err}"]
                    if tok["type"] == "int" and "count" not in tok and "repeat" not in line:
                        env[tok["name"]] = int(values[0])
                if pos != len(parts):
                    return [f"line {idx}: {len(parts) - pos} unexpected extra value(s)"]
        if any(r.strip() for r in rows[idx:]):
            return [f"unexpected extra non-empty line(s) after line {idx}"]
        return []

    def _check_values(self, tok, values: List[str], env) -> Optional[str]:
        name, typ = tok["name"], tok["type"]
        if typ == "word":
            chars = set(CHARSETS[tok.get("charset", "lower")])
            for v in values:
                if not tok.get("min_len", 1) <= len(v) <= tok.get("max_len", 10) or not set(v) <= chars:
                    return f"'{name}' value {v[:20]!r} has the wrong length or characters"
            return None
        pattern = _INT if typ == "int" else _FLOAT
        lo, hi = self._resolve(tok["min"], env), self._resolve(tok["max"], env)
        if lo is None or hi is None:
            return f"bounds of '{name}' not available"
        nums = []
        for v in values:
            if not pattern.match(v):
                return f"'{name}' value {v[:20]!r} is not a valid {typ}"
            x = int(v) if typ == "int" else float(v)
            if not lo <= x <= hi:
                return f"'{name}' value {v} outside [{lo}, {hi}]"
            nums.append(x)
        if tok.get("distinct") and len(set(nums)) != len(nums):
            return f"'{name}' values are not distinct"
        order = tok.get("sorted")
        if order and nums != sorted(nums, reverse=order == "desc"):
            return f"'{name}' values are not sorted ({order})"
        return None

    @staticmethod
    def _resolve(v, env) -> Optional[int]:
        if isinstance(v, str):
            return env.get(v)
        return v

    # -- generation --------------------------------------------------------------
    def generate(self, size: int, rng: random.Random) -> str:
        """One input for ``size``: the size token gets exactly ``size``; without a
        size token, ``size`` bounds the magnitude of the values."""
        return self._generate(int(size), rng, "random")

    def _generate(self, size: int, rng: random.Random, mode: str) -> str:
        if not self.supports_size(size):
            lo, hi = self.size_range()  # type: ignore[misc]
            raise SpecError(f"size {size} is outside the allowed range [{lo}, {hi}] "
                            f"of '{self.size_token['name']}'")  # type: ignore[index]
        env: Dict[str, int] = {}
        out: List[str] = []
        for line in self.lines:
            reps = self._resolve(line.get("repeat", 1), env) or 0
            for _ in range(reps):
                parts: List[str] = []
                for tok in line["tokens"]:
                    n = self._resolve(tok["count"], env) if "count" in tok else 1
                    values = self._values(tok, n or 0, size, rng, mode, env)
                    if tok["type"] == "int" and "count" not in tok and "repeat" not in line:
                        env[tok["name"]] = int(values[0])
                    parts.extend(values)
                out.append(" ".join(parts))
        return "\n".join(out) + "\n"

    def _values(self, tok, n: int, size: int, rng: random.Random, mode: str, env) -> List[str]:
        typ = tok["type"]
        if tok.get("size"):
            return [str(size)]
        if typ == "word":
            chars = CHARSETS[tok.get("charset", "lower")]
            a, b = tok.get("min_len", 1), tok.get("max_len", 10)
            b = min(b, max(a, 8)) if size <= SMALL_SIZE else b
            return ["".join(rng.choice(chars) for _ in range(rng.randint(a, b))) for _ in range(n)]
        lo, hi = self._resolve(tok["min"], env), self._resolve(tok["max"], env)
        if typ == "float":
            d = tok.get("decimals", 2)
            pick = {"min": lambda i: lo, "max": lambda i: hi, "zero": lambda i: min(max(0.0, lo), hi),
                    "alt": lambda i: lo if i % 2 == 0 else hi}.get(mode, lambda i: rng.uniform(lo, hi))
            return [f"{pick(i):.{d}f}" for i in range(n)]
        if tok["name"] in self._length_vars:
            # a length/repeat count: stay near ``size`` instead of up to 10^5+
            return [str(rng.randint(lo, max(lo, min(hi, max(size, 1)))))]
        if mode == "random":
            if self.size_token is None:  # magnitude mode (e.g. "add two integers")
                if max(lo, -size) <= min(hi, size):
                    lo, hi = max(lo, -size), min(hi, size)
            elif size <= SMALL_SIZE and max(lo, -20) <= min(hi, 20):
                lo, hi = max(lo, -20), min(hi, 20)
            if tok.get("distinct"):
                if hi - lo + 1 < n:
                    raise SpecError(f"cannot draw {n} distinct values for '{tok['name']}' from [{lo}, {hi}]")
                nums = rng.sample(range(lo, hi + 1), n)
            else:
                nums = [rng.randint(lo, hi) for _ in range(n)]
        else:
            nums = [{"min": lo, "max": hi, "zero": min(max(0, lo), hi),
                     "alt": lo if i % 2 == 0 else hi}[mode] for i in range(n)]
        order = tok.get("sorted")
        if order:
            nums.sort(reverse=order == "desc")
        return [str(x) for x in nums]

    def boundary_cases(self, rng: random.Random, max_bytes: int = 200_000) -> Dict[str, List[str]]:
        """Deterministic corner-case families. Candidates that break the spec
        (e.g. "all minimum" for a distinct array) are dropped here; the caller
        validates the rest again like any other generated input."""
        r = self.size_range()
        small = min(max(5, r[0]), r[1]) if r else 1000
        plan: List[Tuple[str, int, str]] = []
        if r:
            plan.append(("minimum_size", r[0], "random"))
        plan += [("all_minimum_values", small, "min"), ("all_maximum_values", small, "max"),
                 ("alternating_extremes", small, "alt"), ("all_zero", small, "zero")]
        if r and r[1] != r[0] and r[1] <= 20_000:
            plan.append(("maximum_size", r[1], "random"))
        families: Dict[str, List[str]] = {}
        seen = set()
        for name, size, mode in plan:
            try:
                text = self._generate(size, rng, mode)
            except SpecError:
                continue
            if text in seen or len(text) > max_bytes or self.validate(text):
                continue
            seen.add(text)
            families[name] = [text]
        return families
