import subprocess
from pathlib import Path

import pytest

from autograder import sandbox


def build(tmp_path: Path, code: str) -> str:
    src = tmp_path / "p.c"
    src.write_text(code)
    exe = tmp_path / "p"
    subprocess.run(["cc", "-O0", str(src), "-o", str(exe)], check=True)
    return str(exe)


def test_ok_run_echoes_stdin(tmp_path):
    exe = build(tmp_path, '#include <stdio.h>\nint main(){int a,b;scanf("%d %d",&a,&b);printf("%d\\n",a+b);}')
    r = sandbox.run([exe], "7 5\n", time_limit_s=1)
    assert r.status == "ok" and r.stdout == "12\n"


def test_infinite_loop_is_a_timeout_not_a_hang(tmp_path):
    exe = build(tmp_path, "int main(){volatile int x=0; for(;;) x++;}")
    r = sandbox.run([exe], "", time_limit_s=0.5)
    assert r.status == "timeout"
    assert r.wall_time_s < 5


def test_sleeping_program_hits_the_wall_clock(tmp_path):
    exe = build(tmp_path, "#include <unistd.h>\nint main(){sleep(30);}")
    r = sandbox.run([exe], "", time_limit_s=0.5, wall_limit_s=1)
    assert r.status == "timeout"


def test_segfault_is_a_runtime_error(tmp_path):
    exe = build(tmp_path, "int main(){volatile int *p=0; *p=1; return 0;}")
    r = sandbox.run([exe], "", time_limit_s=1)
    assert r.status == "runtime_error"
    assert r.signal_name in ("SIGSEGV", "SIGBUS")


def test_nonzero_exit(tmp_path):
    exe = build(tmp_path, "int main(){return 3;}")
    r = sandbox.run([exe], "", time_limit_s=1)
    assert r.status == "nonzero_exit" and r.exit_code == 3


def test_output_flood_is_capped(tmp_path):
    exe = build(tmp_path, '#include <stdio.h>\nint main(){for(;;) fputs("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", stdout);}')
    r = sandbox.run([exe], "", time_limit_s=5)
    assert r.status in ("output_limit", "timeout")
    assert len(r.stdout) <= sandbox.OUTPUT_LIMIT_MB * 1024 * 1024


@pytest.mark.skipif(not sandbox.memory_limit_enforced(), reason="RLIMIT_AS is not supported on macOS")
def test_memory_limit(tmp_path):
    exe = build(tmp_path, "#include <stdlib.h>\n#include <string.h>\n"
                          "int main(){char*p=malloc(512u<<20); if(!p) return 7; memset(p,1,512u<<20); return 0;}")
    r = sandbox.run([exe], "", time_limit_s=2, memory_limit_mb=64)
    assert r.status in ("nonzero_exit", "runtime_error", "memory_limit")
