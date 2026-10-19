"""Run untrusted programs under CPU-time, memory, output-size and wall-clock limits.

Limits are applied by a tiny ``/bin/sh`` prelude (``ulimit ...; exec prog``) rather
than a Python ``preexec_fn``: analyzers run in threads, and ``preexec_fn`` can
deadlock a forked child when another thread holds the malloc lock. A deadlocked
child would be killed by the wall clock and reported as a timeout -- a grade that
changes from run to run, which is exactly what this project must never do.

Time-limit verdicts use *CPU* time from ``wait4`` rather than wall-clock time, so a
program that is merely waiting for a busy CPU (other analyzers running in
parallel) is not marked too slow.
"""

from __future__ import annotations

import math
import os
import platform
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

IS_LINUX = platform.system() == "Linux"
IS_DARWIN = platform.system() == "Darwin"

# A fixed, minimal environment. The size of the environment shifts the initial
# stack layout, so keeping it constant makes programs that read uninitialised
# stack memory as repeatable as we can make them.
BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "HOME": "/tmp",
    "TZ": "UTC",
}

OUTPUT_LIMIT_MB = 16


@dataclass
class RunResult:
    status: str  # ok | nonzero_exit | runtime_error | timeout | memory_limit | output_limit
    exit_code: Optional[int]
    signal_name: Optional[str]
    stdout: str
    stderr: str
    cpu_time_s: float
    wall_time_s: float
    max_rss_kb: int

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def describe(self) -> str:
        if self.status == "ok":
            return "exited normally"
        if self.status == "timeout":
            return "exceeded the time limit"
        if self.status == "output_limit":
            return f"printed more than {OUTPUT_LIMIT_MB} MB of output"
        if self.status == "memory_limit":
            return "exceeded the memory limit"
        if self.signal_name:
            return f"crashed ({self.signal_name})"
        return f"exited with code {self.exit_code}"


def _signal_name(sig: int) -> str:
    try:
        return signal.Signals(sig).name
    except ValueError:
        return f"signal {sig}"


def _limit_prelude(time_limit_s: float, memory_limit_mb: Optional[int]) -> str:
    # CPU limit is a backstop only (whole seconds); the TLE verdict is made from
    # the measured CPU time below.
    cpu = max(1, math.ceil(time_limit_s)) + 1
    parts = [f"ulimit -t {cpu}", "ulimit -c 0", f"ulimit -f {OUTPUT_LIMIT_MB * 2048}"]
    # macOS does not support RLIMIT_AS; memory limits are only enforced on Linux.
    if memory_limit_mb and not IS_DARWIN:
        parts.append(f"ulimit -v {int(memory_limit_mb) * 1024}")
    return "; ".join(parts)


def memory_limit_enforced() -> bool:
    return not IS_DARWIN


def run(
    cmd: Sequence[str],
    stdin: Union[str, bytes] = b"",
    *,
    time_limit_s: float,
    memory_limit_mb: Optional[int] = None,
    wall_limit_s: Optional[float] = None,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    scratch_dir: Optional[Path] = None,
) -> RunResult:
    """Run ``cmd`` with ``stdin`` and return what happened. Never raises for
    anything the program itself does (crash, hang, flood of output)."""
    if isinstance(stdin, str):
        stdin = stdin.encode()
    if wall_limit_s is None:
        wall_limit_s = max(3 * time_limit_s, time_limit_s + 2)

    io_dir = Path(tempfile.mkdtemp(prefix="run-", dir=str(scratch_dir) if scratch_dir else None))
    run_cwd = cwd or io_dir / "cwd"
    run_cwd.mkdir(exist_ok=True)
    in_path, out_path, err_path = io_dir / "stdin", io_dir / "stdout", io_dir / "stderr"
    in_path.write_bytes(stdin)

    full_env = dict(BASE_ENV)
    if env:
        full_env.update(env)
    argv = ["/bin/sh", "-c", _limit_prelude(time_limit_s, memory_limit_mb) + '; exec "$0" "$@"', *cmd]

    # stdout/stderr go to files, not pipes: no reader threads, and ulimit -f caps
    # runaway output for us.
    start = time.monotonic()
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
        proc = subprocess.Popen(
            argv, stdin=fin, stdout=fout, stderr=ferr, cwd=str(run_cwd), env=full_env,
            start_new_session=True, close_fds=True,
        )

    killed = threading.Event()

    def _kill() -> None:
        killed.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    timer = threading.Timer(wall_limit_s, _kill)
    timer.daemon = True
    timer.start()
    try:
        _, status, usage = os.wait4(proc.pid, 0)
    finally:
        timer.cancel()
    wall = time.monotonic() - start
    proc.returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
    try:  # take down anything the program forked
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

    cap = OUTPUT_LIMIT_MB * 1024 * 1024
    stdout = _read_capped(out_path, cap)
    stderr = _read_capped(err_path, cap)
    cpu = usage.ru_utime + usage.ru_stime
    rss_kb = usage.ru_maxrss // 1024 if IS_DARWIN else usage.ru_maxrss

    exit_code: Optional[int] = None
    sig_name: Optional[str] = None
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        sig_name = _signal_name(sig)
        if killed.is_set() or sig == signal.SIGXCPU or (sig == signal.SIGKILL and cpu >= time_limit_s):
            verdict = "timeout"
        elif sig == signal.SIGXFSZ:
            verdict = "output_limit"
        else:
            verdict = "runtime_error"
    else:
        exit_code = os.WEXITSTATUS(status)
        if killed.is_set() or cpu > time_limit_s:
            verdict = "timeout"
        elif exit_code != 0:
            verdict = "nonzero_exit"
        else:
            verdict = "ok"
    if (
        verdict in ("runtime_error", "nonzero_exit")
        and memory_limit_mb
        and rss_kb >= 0.9 * memory_limit_mb * 1024
    ):
        verdict = "memory_limit"

    _rmtree(io_dir)
    return RunResult(
        status=verdict, exit_code=exit_code, signal_name=sig_name,
        stdout=stdout, stderr=stderr, cpu_time_s=round(cpu, 4),
        wall_time_s=round(wall, 4), max_rss_kb=int(rss_kb),
    )


def _read_capped(path: Path, cap: int) -> str:
    with open(path, "rb") as f:
        data = f.read(cap)
    return data.decode("utf-8", errors="replace")


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
