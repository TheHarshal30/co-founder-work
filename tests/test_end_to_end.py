"""Grade real submissions end to end (needs a C compiler; no LLM, no network)."""

import pytest

from autograder import tools
from autograder.analyzers import REGISTRY
from autograder.coordinator import grade
from autograder.llm import LLMClient
from autograder.report import render_text
from autograder.rules import load_rules
from tests.conftest import write_rules

# Everything except readability (needs an LLM). Complexity is only included when
# valgrind can count instructions; with CPU time it is not reproducible.
METRICS = [
    {"name": "correctness", "analyzer": "io_tests", "weight": 0.3, "params": {"testset_dir": "tests/"}},
    {"name": "syntax", "analyzer": "compiler_warnings", "weight": 0.1, "params": {"flags": "-Wall -Wextra"}},
    {"name": "structure", "analyzer": "static_analysis", "weight": 0.1,
     "params": {"max_cyclomatic": 8, "max_function_length": 40}},
    {"name": "modularity", "analyzer": "function_split", "weight": 0.1, "params": {"min_functions": 2}},
    {"name": "logical", "analyzer": "edge_case_runner", "weight": 0.2,
     "params": {"cases": ["negatives", "zero", "int_overflow"], "random_tests": 10,
                "sizes": [10, 2147483647], "seed": 1}},
    {"name": "originality", "analyzer": "similarity_flag", "weight": 0.1,
     "params": {"corpus_dir": "submissions/", "known_dir": "known/"}},
    {"name": "memory_safety", "analyzer": "sanitizer_run", "weight": 0.1, "params": {"valgrind": False}},
]


@pytest.fixture
def rules(add_problem):
    metrics = [dict(m) for m in METRICS]
    if tools.which("valgrind"):
        metrics[0]["weight"] = 0.2
        metrics.append({"name": "complexity", "analyzer": "complexity_estimator", "weight": 0.1,
                        "params": {"required": "O(1)", "sizes": [1, 10, 100]}})
    return load_rules(write_rules(add_problem, metrics), REGISTRY)


def offline(tmp_path):
    return LLMClient(cache_dir=tmp_path / "cache", offline=True)


def test_pdf_example_gets_counterexample_for_overflow(rules, add_problem, tmp_path):
    r = grade(add_problem / "submissions" / "add.c", rules, offline(tmp_path))
    correctness = next(m for m in r["metrics"] if m["name"] == "correctness")
    assert correctness["evidence"]["tests_passed"] == 8
    failing = {c["input"].strip(): c for c in correctness["counterexamples"]}
    assert failing["2147483647 1"]["expected"].strip() == "2147483648"
    assert "overflow" in failing["2147483647 1"]["why"].lower()
    modularity = next(m for m in r["metrics"] if m["name"] == "modularity")
    assert modularity["score"] == pytest.approx(0.3)  # only main(): 0.6 * 1/2
    assert "Where you failed" in render_text(r)


def test_clean_solution_scores_full_marks_on_objective_metrics(rules, add_problem, tmp_path):
    r = grade(add_problem / "submissions" / "add_clean.c", rules, offline(tmp_path))
    scores = {m["name"]: m["score"] for m in r["metrics"]}
    for name in ("correctness", "logical", "modularity", "memory_safety"):
        assert scores[name] == 1.0, (name, r["metrics"])


def test_infinite_loop_gives_partial_report(rules, add_problem, tmp_path):
    r = grade(add_problem / "submissions" / "add_infinite_loop.c", rules, offline(tmp_path))
    assert r["first_pass"]["suspected_infinite_loop"] is True
    assert r["partial"] is True
    by_name = {m["name"]: m for m in r["metrics"]}
    assert by_name["correctness"]["status"] == "skipped" and by_name["correctness"]["score"] == 0
    assert by_name["modularity"]["status"] == "ok"  # source-only analyzers still ran
    assert r["flags"][0]["type"] == "suspected_infinite_loop"


def test_compile_error(rules, add_problem, tmp_path):
    r = grade(add_problem / "submissions" / "add_compile_error.c", rules, offline(tmp_path))
    assert r["first_pass"]["compiled"] is False
    assert any(f["type"] == "compile_failed" for f in r["flags"])
    syntax = next(m for m in r["metrics"] if m["name"] == "syntax")
    assert syntax["score"] == 0.0


def test_copied_submission_is_flagged(rules, add_problem, tmp_path):
    r = grade(add_problem / "submissions" / "add_copied.c", rules, offline(tmp_path))
    flags = [f for f in r["flags"] if f["type"] == "suspected_plagiarism"]
    assert flags and flags[0]["match"] == "submissions/add_clean.c"


def test_missing_llm_marks_grade_incomplete_instead_of_guessing(add_problem, tmp_path):
    rules = load_rules(write_rules(add_problem, [
        {"name": "correctness", "analyzer": "io_tests", "weight": 0.5, "params": {"testset_dir": "tests/"}},
        {"name": "readability", "analyzer": "llm_review", "weight": 0.5, "params": {}},
    ]), REGISTRY)
    r = grade(add_problem / "submissions" / "add_clean.c", rules, offline(tmp_path))
    assert r["grade_complete"] is False
    assert r["grade"] == 100.0  # scaled over the measured half
    assert any(f["type"] == "incomplete_grade" for f in r["flags"])


def test_same_submission_same_rules_same_report(rules, add_problem, tmp_path):
    """The contract: same submission + same rules -> same grade (and same feedback)."""
    sub = add_problem / "submissions" / "add.c"
    a = grade(sub, rules, offline(tmp_path))
    b = grade(sub, rules, offline(tmp_path))
    assert a["grade"] == b["grade"]
    assert a["fingerprint"] == b["fingerprint"]


def test_changing_the_rules_changes_the_rules_hash(rules, add_problem, tmp_path):
    a = grade(add_problem / "submissions" / "add.c", rules, offline(tmp_path))
    raw = rules.path.read_text().replace('"max_function_length": 40', '"max_function_length": 41')
    rules.path.write_text(raw)
    b = grade(add_problem / "submissions" / "add.c", load_rules(rules.path, REGISTRY), offline(tmp_path))
    assert a["rules_hash"] != b["rules_hash"]
