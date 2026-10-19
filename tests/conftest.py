import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADD = ROOT / "problems" / "add_two_integers"
MAXSUB = ROOT / "problems" / "max_subarray"


@pytest.fixture
def add_problem(tmp_path):
    """A private copy of the add_two_integers problem folder."""
    dst = tmp_path / "add_two_integers"
    shutil.copytree(ADD, dst)
    return dst


def write_rules(folder: Path, metrics, **top) -> Path:
    rules = {"problem": "t", "time_limit_s": 1, "memory_limit_mb": 64, "sample_input": "7 5\n",
             "reference": "reference.c", "include_usefulness_limitations": False, **top,
             "metrics": metrics}
    path = folder / "rules.test.json"
    path.write_text(json.dumps(rules, indent=2))
    return path
