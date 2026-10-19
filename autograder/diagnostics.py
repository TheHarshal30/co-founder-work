"""Parse AddressSanitizer / UBSan / LeakSanitizer / valgrind output into issues.

Raw sanitizer output contains addresses and PIDs that change on every run, so
only stable facts (issue kind, message without addresses, file:line) are kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .sandbox import IS_DARWIN


@dataclass(frozen=True)
class Issue:
    tool: str  # asan | ubsan | lsan | valgrind
    kind: str
    message: str
    location: Optional[str]  # "file.c:12"

    def as_dict(self) -> dict:
        return {"tool": self.tool, "kind": self.kind, "message": self.message,
                "location": self.location}

    def short(self) -> str:
        loc = f"{self.location}: " if self.location else ""
        return f"{loc}{self.message}"


SANITIZER_ENV = {
    # LeakSanitizer is not supported on macOS/arm64; enabling it there aborts ASan.
    "ASAN_OPTIONS": ("detect_leaks=0" if IS_DARWIN else "detect_leaks=1")
    + ":halt_on_error=1:abort_on_error=0:exitcode=23:allocator_may_return_null=1",
    "UBSAN_OPTIONS": "print_stacktrace=0:halt_on_error=0",
    "LSAN_OPTIONS": "exitcode=23",
}

_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_UBSAN = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+): runtime error: (?P<msg>.+)$", re.M)
_ASAN = re.compile(r"ERROR: AddressSanitizer: (?P<kind>[\w-]+)(?P<rest>[^\n]*)")
_ACCESS = re.compile(r"^(READ|WRITE) of size (\d+)", re.M)
_FRAME = re.compile(r"#\d+ 0x[0-9a-fA-F]+ in (?P<fn>\S+) (?P<path>[^\s:]+):(?P<line>\d+)")
_LEAK = re.compile(r"^(?P<what>(?:Direct|Indirect) leak of \d+ byte\(s\) in \d+ object\(s\)) allocated from:"
                   r"(?P<frames>(?:\n\s+#\d+[^\n]*)*)", re.M)
_VG_PID = re.compile(r"^==\d+== ?", re.M)
_VG_FRAME = re.compile(r"(?:at|by) 0x[0-9A-Fa-f]+: (?P<fn>\S+) \((?P<file>[^:)]+):(?P<line>\d+)\)")
_VG_ERRORS = [
    (re.compile(r"^Invalid (read|write) of size (\d+)", re.M), "invalid-{0}"),
    (re.compile(r"^Conditional jump or move depends on uninitialised value", re.M), "uninitialised-value"),
    (re.compile(r"^Use of uninitialised value of size \d+", re.M), "uninitialised-value"),
    (re.compile(r"^Invalid free\(\)", re.M), "invalid-free"),
    (re.compile(r"^Mismatched free\(\)", re.M), "mismatched-free"),
]
_VG_LOST = re.compile(r"definitely lost: ([\d,]+) bytes in ([\d,]+) blocks")
_VG_RECORD = re.compile(r"^[\d,]+ (?:\([^)]*\) )?bytes in [\d,]+ blocks are definitely lost", re.M)


def _ub_kind(msg: str) -> str:
    m = msg.lower()
    for needle, kind in [
        ("signed integer overflow", "signed-integer-overflow"),
        ("out of bounds", "array-index-out-of-bounds"),
        ("insufficient space for an object", "object-size-overflow"),
        ("null pointer", "null-pointer-dereference"),
        ("shift exponent", "invalid-shift"),
        ("left shift", "invalid-shift"),
        ("division by zero", "division-by-zero"),
        ("misaligned", "misaligned-access"),
        ("not a valid value for type", "invalid-value"),
    ]:
        if needle in m:
            return kind
    return "undefined-behaviour"


def _user_frame(text: str, source_name: str) -> Optional[str]:
    for m in _FRAME.finditer(text):
        if m.group("path").rsplit("/", 1)[-1] == source_name:
            return f"{source_name}:{m.group('line')}"
    return None


def parse_sanitizer(stderr: str, source_name: str) -> List[Issue]:
    issues: List[Issue] = []
    for m in _UBSAN.finditer(stderr):
        if m.group("file").rsplit("/", 1)[-1] != source_name:
            continue
        msg = _ADDR.sub("0x…", m.group("msg").strip())
        issues.append(Issue("ubsan", _ub_kind(msg), f"runtime error: {msg}",
                            f"{source_name}:{m.group('line')}"))
    for m in _ASAN.finditer(stderr):
        kind = m.group("kind")
        block = stderr[m.end():]
        access = _ACCESS.search(block[:400])
        msg = kind + (f" ({access.group(1)} of size {access.group(2)})" if access else "")
        issues.append(Issue("asan", kind, msg, _user_frame(block, source_name)))
    for m in _LEAK.finditer(stderr):
        issues.append(Issue("lsan", "memory-leak", m.group("what"),
                            _user_frame(m.group("frames"), source_name)))
    return _dedupe(issues)


def parse_valgrind(stderr: str, source_name: str) -> List[Issue]:
    text = _VG_PID.sub("", stderr)
    issues: List[Issue] = []
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        for pattern, kind_fmt in _VG_ERRORS:
            m = pattern.search(block)
            if not m:
                continue
            kind = kind_fmt.format(*(g for g in m.groups() if g is not None)) if "{0}" in kind_fmt else kind_fmt
            loc = None
            for f in _VG_FRAME.finditer(block):
                if f.group("file") == source_name:
                    loc = f"{source_name}:{f.group('line')}"
                    break
            issues.append(Issue("valgrind", kind, m.group(0).strip(), loc))
            break
    records = 0
    for block in blocks:
        m = _VG_RECORD.search(block)
        if not m:
            continue
        records += 1
        loc = next((f"{source_name}:{f.group('line')}" for f in _VG_FRAME.finditer(block)
                    if f.group("file") == source_name), None)
        issues.append(Issue("valgrind", "memory-leak", m.group(0).strip(), loc))
    lost = _VG_LOST.search(text)
    if not records and lost and lost.group(1) != "0":
        issues.append(Issue("valgrind", "memory-leak",
                            f"definitely lost: {lost.group(1)} bytes in {lost.group(2)} blocks", None))
    return _dedupe(issues)


def _dedupe(issues: List[Issue]) -> List[Issue]:
    seen, out = set(), []
    for i in issues:
        key = (i.kind, i.location)
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out
