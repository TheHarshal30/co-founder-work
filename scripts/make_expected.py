"""Write tests/NN.out next to every tests/NN.in using the reference solution.

    python scripts/make_expected.py problems/add_two_integers

Instructors only write inputs; expected outputs come from reference.c, so they
can never disagree with it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autograder.context import ReferenceSolution  # noqa: E402
from autograder.rules import load_rules  # noqa: E402


def main() -> int:
    pdir = Path(sys.argv[1])
    rules = load_rules(pdir / "rules.json")
    ref = ReferenceSolution.for_rules(rules)
    folders = {m.params["testset_dir"] for m in rules.metrics if m.analyzer == "io_tests"}
    for folder in sorted(folders):
        for case in sorted(rules.resolve(folder).glob("*.in")):
            case.with_suffix(".out").write_text(ref.output(case.read_text()))
            print(f"wrote {case.with_suffix('.out')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
