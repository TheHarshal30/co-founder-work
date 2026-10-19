import pytest

from autograder.analyzers import REGISTRY
from autograder.rules import RulesError, load_rules
from tests.conftest import ADD, MAXSUB, write_rules


@pytest.mark.parametrize("folder", [ADD, MAXSUB])
def test_shipped_rule_sets_are_valid(folder):
    rules = load_rules(folder / "rules.json", REGISTRY)
    assert abs(sum(m.weight for m in rules.metrics) - 1.0) < 1e-9


def test_weights_must_sum_to_one(add_problem):
    path = write_rules(add_problem, [
        {"name": "a", "analyzer": "function_split", "weight": 0.5},
        {"name": "b", "analyzer": "compiler_warnings", "weight": 0.4},
    ])
    with pytest.raises(RulesError, match="sum to 1.00"):
        load_rules(path, REGISTRY)


def test_unknown_analyzer_lists_available_ones(add_problem):
    path = write_rules(add_problem, [{"name": "a", "analyzer": "magic", "weight": 1.0}])
    with pytest.raises(RulesError, match="unknown analyzer 'magic'.*io_tests"):
        load_rules(path, REGISTRY)


def test_analyzer_params_are_checked_at_load_time(add_problem):
    path = write_rules(add_problem, [
        {"name": "c", "analyzer": "complexity_estimator", "weight": 0.5,
         "params": {"required": "O(n!)", "sizes": [1, 2]}},
        {"name": "e", "analyzer": "edge_case_runner", "weight": 0.5,
         "params": {"cases": ["negatives", "no_such_family"]}},
    ])
    with pytest.raises(RulesError) as e:
        load_rules(path, REGISTRY)
    text = str(e.value)
    assert "'required' must be one of" in text
    assert "at least 3" in text
    assert "no_such_family" in text


def test_bad_json_reports_the_line(add_problem):
    path = add_problem / "broken.json"
    path.write_text('{\n  "problem": "x",\n  "metrics": [,]\n}')
    with pytest.raises(RulesError, match="line 3"):
        load_rules(path)


def test_relative_paths_resolve_against_the_rules_file(add_problem):
    rules = load_rules(add_problem / "rules.json", REGISTRY)
    assert rules.reference == (add_problem / "reference.c").resolve()
    assert rules.resolve("tests/").is_dir()


def test_plugin_analyzer_from_rules_file(add_problem):
    from autograder.coordinator import grade
    from autograder.llm import LLMClient
    from tests.conftest import ROOT

    path = write_rules(add_problem, [
        {"name": "conciseness", "analyzer": "line_budget", "weight": 1.0, "params": {"max_code_lines": 5}},
    ], plugins=[str(ROOT / "examples" / "plugins" / "line_budget.py")])
    rules = load_rules(path, REGISTRY)
    report = grade(add_problem / "submissions" / "add.c", rules, LLMClient(offline=True))
    assert report["metrics"][0]["evidence"]["code_lines"] == 7
    assert report["grade"] == 60.0
