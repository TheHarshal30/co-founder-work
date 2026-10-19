"""Unit tests for the pure-Python parts: no compiler needed."""

import math

from autograder.analyzers.complexity_estimator import estimate_class, normalize_class
from autograder.analyzers.similarity_flag import dice, fingerprints, line_ranges
from autograder.analyzers.static_analysis import mutable_globals
from autograder.compare import outputs_match, verdict
from autograder.ctokens import normalized, readability_facts, strip_comments, tokenize
from autograder.diagnostics import parse_sanitizer, parse_valgrind
from autograder.explain import explain
from autograder.sandbox import RunResult


def run(stdout="", status="ok", sig=None, code=0):
    return RunResult(status, code, sig, stdout, "", 0.01, 0.01, 1000)


# -- output comparison ----------------------------------------------------
def test_trim_mode_ignores_trailing_whitespace_only():
    assert outputs_match("12\n", "12   \n\n", "trim")
    assert not outputs_match("12\n", " 12\n", "exact")
    assert not outputs_match("1 2\n", "1  2\n", "trim")
    assert outputs_match("1 2\n", "1  2\n", "tokens")


def test_float_mode():
    assert outputs_match("3.14159\n", "3.141590001\n", "float:1e-6")
    assert not outputs_match("3.14\n", "3.15\n", "float:1e-6")


def test_verdicts():
    assert verdict(run("12\n"), "12\n") == (True, "pass")
    assert verdict(run("Sum = 12\n"), "12\n") == (False, "wrong_answer")
    assert verdict(run("1  2\n"), "1 2\n") == (False, "presentation_error")
    assert verdict(run("", "timeout"), "12\n") == (False, "timeout")
    assert verdict(run("12\n", "nonzero_exit", code=1), "12\n") == (False, "nonzero_exit")


# -- explanations ---------------------------------------------------------
def test_wraparound_is_explained_as_overflow():
    why, fix = explain("wrong_answer", "2147483648\n", "-2147483648\n", run())
    assert "2^32" in why and "long long" in fix


def test_zero_initialised_maximum():
    why, fix = explain("wrong_answer", "-2\n", "0\n", run())
    assert "initialised to 0" in why and "first element" in fix


def test_extra_text_around_answer():
    why, _ = explain("wrong_answer", "12\n", "Sum = 12\n", run())
    assert "extra text" in why


def test_timeout_on_large_input_blames_speed_not_loops():
    _, fix_small = explain("timeout", "", "", run(status="timeout"), input_len=10)
    _, fix_large = explain("timeout", "", "", run(status="timeout"), input_len=10**6)
    assert "never met" in fix_small and "too slow" in fix_large


# -- complexity fitting ---------------------------------------------------
SIZES = [500, 1000, 2000, 4000, 8000]


def test_estimates_classic_growth_rates():
    base = 100_000
    cases = {
        "O(1)": [base + 3 for n in SIZES],
        "O(n)": [base + 40 * n for n in SIZES],
        "O(n log n)": [base + 40 * n * math.log2(n) for n in SIZES],
        "O(n^2)": [base + 3 * n * n for n in SIZES],
    }
    for expected, costs in cases.items():
        assert estimate_class(SIZES, costs)[0] == expected, expected


def test_startup_cost_does_not_hide_linear_growth():
    # 1M instructions of libc start-up dwarf a cheap loop at small n
    assert estimate_class(SIZES, [1_000_000 + 30 * n for n in SIZES])[0] == "O(n)"


def test_complexity_names_are_normalised():
    assert normalize_class("O(n log n)") == normalize_class("o(nlogn)") == "O(n log n)"
    assert normalize_class("O(n²)") == "O(n^2)"
    assert normalize_class("O(n!)") is None


# -- tokens, similarity ----------------------------------------------------
PROG = """#include <stdio.h>
/* sum two numbers */
int main(void) {
    int first, second;   // inputs
    scanf("%d %d", &first, &second);
    printf("%d\\n", first + second);
    return 0;
}
"""


def test_tokenizer_separates_directives_comments_and_code():
    kinds = [t.kind for t in tokenize(PROG)]
    assert kinds[0] == "pp" and kinds.count("comment") == 2
    assert "sum two numbers" not in strip_comments(PROG)
    assert strip_comments(PROG).count("\n") == PROG.count("\n")


def test_renaming_and_reformatting_does_not_change_fingerprints():
    renamed = PROG.replace("first", "x").replace("second", "y").replace("    ", "\t")
    renamed = renamed.replace("// inputs", "").replace("0;", "0 ;")
    assert dice(set(fingerprints(PROG)), set(fingerprints(renamed))) == 1.0
    assert [t.text for t in normalized(tokenize(PROG))][:4] == ["int", "I", "(", "void"]


def test_different_programs_are_not_similar():
    other = """int fib(int n){ if (n < 2) return n; return fib(n-1) + fib(n-2); }
    int main(void){ for (int i = 0; i < 10; i++) printf("%d ", fib(i)); return 0; }"""
    assert dice(set(fingerprints(PROG)), set(fingerprints(other))) < 0.3


def test_line_ranges():
    assert line_ranges([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10"


def test_readability_facts():
    facts = readability_facts(PROG)
    assert facts["comment_lines"] == 2
    assert facts["single_letter_identifiers"] == []
    assert "first" in facts["identifiers"]


def test_mutable_globals_found_but_not_constants_or_prototypes():
    code = "int counter;\nconst int LIMIT = 3;\nint helper(int);\nstatic double avg = 0;\nint main(void){int local;return 0;}"
    assert [g["name"] for g in mutable_globals(code)] == ["counter", "avg"]


# -- sanitizer / valgrind parsing -------------------------------------------
UBSAN = "add.c:5:23: runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'\n"
ASAN = """=================================================================
==12==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x502000000018 at pc 0xaaaa bp 0xffff sp 0xfffe
WRITE of size 8 at 0x502000000018 thread T0
    #0 0xaaaaaaaa in __isoc99_scanf (/lib/libasan.so.8+0x1234)
    #1 0xaaaaaaab in main /tmp/autograder-x/src/leaky.c:10
"""
VALGRIND = """==7== Invalid read of size 8
==7==    at 0x1091F4: main (leaky.c:13)
==7==
==7== 72 bytes in 1 blocks are definitely lost in loss record 1 of 1
==7==    at 0x48850C8: malloc (vg_replace_malloc.c:381)
==7==    by 0x109197: main (leaky.c:8)
==7==
==7== LEAK SUMMARY:
==7==    definitely lost: 72 bytes in 1 blocks
"""


def test_parse_ubsan_and_asan():
    ub = parse_sanitizer(UBSAN, "add.c")
    assert ub[0].kind == "signed-integer-overflow" and ub[0].location == "add.c:5"
    asan = parse_sanitizer(ASAN, "leaky.c")
    assert asan[0].kind == "heap-buffer-overflow"
    assert asan[0].location == "leaky.c:10"
    assert "WRITE of size 8" in asan[0].message
    assert "0x5020" not in asan[0].message  # addresses are dropped (they change every run)


def test_parse_valgrind():
    issues = parse_valgrind(VALGRIND, "leaky.c")
    kinds = {(i.kind, i.location) for i in issues}
    assert ("invalid-read", "leaky.c:13") in kinds
    assert ("memory-leak", "leaky.c:8") in kinds
    assert len(issues) == 2  # the summary line does not count a second time
