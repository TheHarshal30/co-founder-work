"""Compile C sources and parse compiler diagnostics."""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .sandbox import BASE_ENV

COMPILE_TIMEOUT_S = 60

_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?:(?P<col>\d+):)? "
    r"(?P<kind>warning|error|fatal error): (?P<msg>.*?)(?: \[(?P<flag>-W[^\]]+)\])?$",
    re.M,
)


@dataclass
class Diagnostic:
    line: int
    kind: str  # warning | error
    message: str
    flag: Optional[str]

    def as_dict(self) -> dict:
        return {"line": self.line, "kind": self.kind, "message": self.message, "flag": self.flag}


@dataclass
class BuildResult:
    ok: bool
    binary: Optional[Path]
    command: List[str]
    output: str
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def warnings(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == "warning"]

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind != "warning"]


def split_flags(flags) -> List[str]:
    if flags is None:
        return []
    if isinstance(flags, str):
        return shlex.split(flags)
    return [str(f) for f in flags]


def parse_diagnostics(text: str, source_name: str) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    seen = set()
    for m in _DIAG_RE.finditer(text):
        if Path(m.group("file")).name != source_name:
            continue  # notes from system headers etc.
        kind = "error" if m.group("kind") != "warning" else "warning"
        flag = m.group("flag")
        # -Werror turns warnings into errors; they are still warnings in spirit.
        if flag and flag.startswith("-Werror="):
            kind, flag = "warning", "-W" + flag[len("-Werror="):]
        key = (int(m.group("line")), kind, m.group("msg"), flag)
        if key in seen:
            continue
        seen.add(key)
        out.append(Diagnostic(int(m.group("line")), kind, m.group("msg").strip(), flag))
    return out


def compile_c(
    cc: str,
    source: Path,
    output: Optional[Path],
    flags: Sequence[str] = (),
    ldflags: Sequence[str] = (),
) -> BuildResult:
    """Compile ``source``. With ``output=None`` only compiles to an object
    (used to collect warnings without linking)."""
    if output is None:
        cmd = [cc, *flags, "-c", source.name, "-o", "/dev/null"]
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [cc, *flags, source.name, "-o", str(output), *ldflags]
    env = dict(BASE_ENV)
    env["TMPDIR"] = str(source.parent)
    try:
        proc = subprocess.run(
            cmd, cwd=str(source.parent), capture_output=True, text=True,
            timeout=COMPILE_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        return BuildResult(False, None, cmd, f"compiler timed out after {COMPILE_TIMEOUT_S}s")
    except FileNotFoundError:
        return BuildResult(False, None, cmd, f"compiler not found: {cc}")
    text = (proc.stdout + proc.stderr).replace(str(source.parent) + "/", "")
    diags = parse_diagnostics(text, source.name)
    ok = proc.returncode == 0 and (output is None or output.exists())
    return BuildResult(ok, output if ok else None, cmd, text.strip(), diags)
