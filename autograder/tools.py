"""Find external tools and record their versions (a grade is only reproducible
together with the toolchain that produced it)."""

from __future__ import annotations

import functools
import platform
import re
import shutil
import subprocess
from typing import Dict, Optional


@functools.lru_cache(maxsize=None)
def which(name: str) -> Optional[str]:
    return shutil.which(name)


def _first_line(cmd: list) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else None


@functools.lru_cache(maxsize=None)
def version(tool: str) -> Optional[str]:
    if tool == "lizard":
        try:
            import lizard  # type: ignore

            return str(getattr(lizard, "version", "unknown"))
        except ImportError:
            return None
    if not which(tool):
        return None
    line = _first_line([tool, "--version"])
    if not line:
        return None
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", line)
    if tool in ("gcc", "cc", "clang"):
        # keep the vendor string: Apple clang and GNU gcc are different compilers
        return line.strip()
    return m.group(1) if m else line.strip()


def snapshot(cc: str) -> Dict[str, Optional[str]]:
    """Versions of everything that can influence a grade."""
    return {
        "platform": f"{platform.system()}-{platform.machine()}",
        "python": platform.python_version(),
        "cc": version(cc),
        "valgrind": version("valgrind"),
        "cppcheck": version("cppcheck"),
        "lizard": version("lizard"),
    }
