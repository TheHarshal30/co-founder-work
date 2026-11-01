"""Asset generation: submission.c + rules.json only (LLM mocked, needs a C compiler).

The fake LLM answers by recognising which step is asking (input spec, corner
cases, oracle program, known solution), so every test is deterministic and
offline. It also records every prompt, so tests can prove the submission is
never shown to the model.
"""

import json
import random
import shutil

import pytest

from autograder import cli
from autograder.analyzers import REGISTRY
from autograder.assets import AssetStore, find_missing, prepare_assets
from autograder.assets.inputspec import InputSpec, SpecError, check_spec
from autograder.assets.oracle import screen_c_source
from autograder.assets.prepare import _safe_path, materialize
from autograder.assets.store import AssetRecord
from autograder.coordinator import grade
from autograder.llm import LLMAnswer, LLMClient, LLMUnavailable
from autograder.rules import hash_paths, load_rules
from tests.conftest import ADD

ADD_SPEC = {"version": 1, "lines": [{"tokens": [
    {"name": "a", "type": "int", "min": -2147483648, "max": 2147483647},
    {"name": "b", "type": "int", "min": -2147483648, "max": 2147483647}]}]}
GOOD_ADD = '#include <stdio.h>\nint main(void){long long a,b;if(scanf("%lld %lld",&a,&b)!=2)return 1;' \
           'printf("%lld\\n",a+b);return 0;}\n'
BRUTE_ADD = '#include <stdio.h>\nint main(void){long long a,b,s=0;if(scanf("%lld %lld",&a,&b)!=2)return 1;' \
            's=a;s+=b;printf("%lld\\n",s);return 0;}\n'
CAREFUL_ADD = '#include <stdio.h>\n#include <inttypes.h>\nint main(void){int64_t a,b;' \
              'if(scanf("%" SCNd64 " %" SCNd64,&a,&b)!=2)return 1;printf("%" PRId64 "\\n",a+b);return 0;}\n'
INT_ADD = '#include <stdio.h>\nint main(void){int a,b;if(scanf("%d %d",&a,&b)!=2)return 1;' \
          'printf("%d\\n",a+b);return 0;}\n'

METRICS = [
    {"name": "correctness", "analyzer": "io_tests", "weight": 0.3, "params": {"testset_dir": "tests/"}},
    {"name": "syntax", "analyzer": "compiler_warnings", "weight": 0.1, "params": {"flags": "-Wall -Wextra"}},
    {"name": "modularity", "analyzer": "function_split", "weight": 0.1, "params": {"min_functions": 2}},
    {"name": "logical", "analyzer": "edge_case_runner", "weight": 0.2,
     "params": {"cases": "all", "random_tests": 6, "sizes": [10, 2147483647], "seed": 1}},
    {"name": "complexity", "analyzer": "complexity_estimator", "weight": 0.1,
     "params": {"required": "O(1)", "sizes": [1, 10, 100], "method": "cpu_time", "repeats": 1}},
    {"name": "memory_safety", "analyzer": "sanitizer_run", "weight": 0.1, "params": {"valgrind": False}},
    {"name": "originality", "analyzer": "similarity_flag", "weight": 0.1,
     "params": {"corpus_dir": "submissions/", "known_dir": "known/"}},
]


class FakeLLM(LLMClient):
    def __init__(self, tmp_path, spec=ADD_SPEC, families=None, programs=None, digest=None):
        super().__init__(cache_dir=tmp_path / "fake-cache", offline=False)  # chat_json never hits the network
        self.spec_answers = list(spec) if isinstance(spec, list) else [spec]
        self.families = families if families is not None else [
            {"name": "overflow", "why": "sum exceeds int", "inputs": ["2147483647 1", "-2147483648 -1\n"]},
            {"name": "bad", "why": "invalid on purpose", "inputs": ["1 2 3\n", "99999999999 1\n"]},
        ]
        self.programs = programs or {"simplest": BRUTE_ADD, "efficient": GOOD_ADD, "careful": CAREFUL_ADD}
        self.digest = digest
        self.calls = []

    def model_identity(self, provider, model):
        return self.digest

    def chat_json(self, *, provider, model, messages, temperature=0.0, seed=42, schema=None, cache_salt=None):
        self.calls.append(messages)
        system, user = messages[0]["content"], messages[1]["content"]
        if "INPUT FORMAT" in system:
            attempt = sum(m["role"] == "assistant" for m in messages)
            data = {"spec": self.spec_answers[min(attempt, len(self.spec_answers) - 1)]}
        elif "CORNER-CASE" in system:
            data = {"families": self.families}
        elif "complete C11 program" in system:
            key = next(k for k in self.programs if k in user.split("\n")[0].lower())
            data = {"code": self.programs[key]}
        else:
            raise LLMUnavailable("unexpected prompt")
        if isinstance(data.get("spec"), Exception):
            raise data["spec"]
        return LLMAnswer(data, json.dumps(data), f"fake-{len(self.calls)}", False, self.digest)


def minimal_problem(tmp_path, metrics=METRICS, **ag):
    """A folder with ONLY rules.json + one submission (no tests, reference,
    generator, corner cases, statement file or corpus)."""
    d = tmp_path / "minimal"
    d.mkdir()
    shutil.copy(ADD / "submissions" / "add.c", d / "submission.c")
    rules = {"problem": "add_two_integers", "time_limit_s": 1, "memory_limit_mb": 64,
             "sample_input": "7 5\n", "reference": "reference.c", "include_usefulness_limitations": False,
             "asset_generation": {"description": (ADD / "problem.md").read_text(), **ag},
             "metrics": metrics}
    (d / "rules.json").write_text(json.dumps(rules, indent=2))
    return d


def prepare(d, llm, tmp_path):
    store = AssetStore(tmp_path / "cache" / "autograder.sqlite3")
    return prepare_assets(d / "rules.json", llm, store, tmp_path / "cache"), store


def graded(d, llm, tmp_path):
    prep, store = prepare(d, llm, tmp_path)
    rules = load_rules(d / "rules.json", REGISTRY, prep.overlay)
    return grade(d / "submission.c", rules, llm), prep, store


# -- backward compatibility ------------------------------------------------------
def test_complete_problem_needs_no_preparation_and_grades_identically(add_problem, tmp_path):
    """--generate-assets on a problem that has everything = the explicit workflow."""
    from tests.test_end_to_end import METRICS as E2E
    from tests.conftest import write_rules

    path = write_rules(add_problem, E2E)
    llm = FakeLLM(tmp_path)
    store = AssetStore(tmp_path / "db.sqlite3")
    prep = prepare_assets(path, llm, store, tmp_path / "cache")
    assert prep.overlay is None and llm.calls == []
    explicit = grade(add_problem / "submissions" / "add.c", load_rules(path, REGISTRY), llm)
    auto = grade(add_problem / "submissions" / "add.c", load_rules(path, REGISTRY, prep.overlay), llm)
    assert auto["fingerprint"] == explicit["fingerprint"]
    assert "asset_preparation" not in auto


def test_explicit_workflow_still_rejects_missing_assets(tmp_path):
    from autograder.rules import RulesError

    d = minimal_problem(tmp_path)
    with pytest.raises(RulesError, match="reference solution not found"):
        load_rules(d / "rules.json", REGISTRY)


# -- the minimal workflow, oracle turned off -------------------------------------------
def test_minimal_workflow_without_oracle_runs_what_it_can_and_awards_nothing_else(tmp_path):
    d = minimal_problem(tmp_path, oracle="none")
    before = hash_paths([d])
    r, prep, store = graded(d, FakeLLM(tmp_path), tmp_path)
    by = {m["name"]: m for m in r["metrics"]}

    # no trusted expected outputs -> correctness and corner cases are NOT performed
    for name in ("correctness", "logical"):
        assert by[name]["status"] == "not_performed" and by[name]["score"] is None
    # metrics that need no expected outputs still run on generated, validated assets
    assert by["complexity"]["status"] == "ok" and by["complexity"]["score"] is not None
    assert by["memory_safety"]["evidence"]["inputs_checked"] > 1
    assert by["syntax"]["status"] == "ok" and by["modularity"]["status"] == "ok"
    assert by["originality"]["status"] == "ok"  # against generated known LLM solutions

    # no credit for tests not performed: the 50 missing points stay in the denominator
    earned = sum(m["contribution"] for m in r["metrics"] if m["contribution"] is not None)
    assert r["grade"] == pytest.approx(round(earned, 1))
    assert r["grade_complete"] is False
    assert any(f["type"] == "tests_not_performed" for f in r["flags"])
    assert r["asset_preparation"]["expected_outputs_from"] == "none"
    # the instructor's folder is untouched; generated files live in the cache
    assert hash_paths([d]) == before
    assert prep.bundle_dir.is_relative_to(tmp_path / "cache")


def test_submission_is_never_shown_to_the_model_nor_used_as_answer_key(tmp_path):
    d = minimal_problem(tmp_path)
    llm = FakeLLM(tmp_path)
    r, prep, store = graded(d, llm, tmp_path)
    source = (d / "submission.c").read_text()
    prompts = "\n".join(m["content"] for call in llm.calls for m in call)
    assert source.strip() not in prompts and 'printf("%d\\n",a+b)' not in prompts
    outs = [a for a in store.assets(prep.bundle_key) if a.kind == "test_output"]
    # every expected output is the instructor's or the validated oracle's, never the submission's
    assert outs and {a.origin for a in outs} <= {"instructor_example", "llm_oracle"}
    assert "2147483648" in next(a.content for a in outs if a.name.endswith("overflow.out"))  # add.c prints a wrapped value


def test_instructor_examples_give_partial_correctness(tmp_path):
    d = minimal_problem(tmp_path, oracle="none", examples=[{"input": "2147483647 1\n", "output": "2147483648\n"},
                                            {"input": "-3 -4\n", "output": "-7\n"}])
    rules = json.loads((d / "rules.json").read_text())
    rules["sample_output"] = "12\n"
    (d / "rules.json").write_text(json.dumps(rules))
    r, prep, _ = graded(d, FakeLLM(tmp_path), tmp_path)
    c = next(m for m in r["metrics"] if m["name"] == "correctness")
    assert c["status"] == "ok" and c["evidence"]["tests_total"] == 3  # sample + 2 examples, nothing invented
    assert c["evidence"]["tests_passed"] == 2  # add.c overflows on 2147483647 + 1
    assert r["asset_preparation"]["metrics"]["correctness"]["status"] == "partial"


# -- the LLM oracle (on by default) -------------------------------------------------------
EXAMPLES = [{"input": "7 5\n", "output": "12\n"}, {"input": "2147483647 1\n", "output": "2147483648\n"}]


def test_consensus_oracle_enables_correctness_and_finds_overflow(tmp_path):
    d = minimal_problem(tmp_path, oracle="llm_consensus", examples=EXAMPLES)
    r, prep, store = graded(d, FakeLLM(tmp_path), tmp_path)
    by = {m["name"]: m for m in r["metrics"]}
    assert r["asset_preparation"]["expected_outputs_from"] == "llm_consensus_oracle"
    assert by["correctness"]["status"] == "ok" and by["correctness"]["score"] < 1
    assert by["logical"]["status"] == "ok" and by["logical"]["score"] < 1
    assert any("overflow" in c["why"].lower() for c in r["counterexamples"])
    assert any(f["type"] == "llm_oracle_used" for f in r["flags"])
    oracle = [a for a in store.assets(prep.bundle_key) if a.kind == "oracle"]
    assert [a.status for a in oracle].count("validated") == 3  # all three agreed
    assert [a.path for a in store.assets(prep.bundle_key) if a.kind == "reference"] == ["reference/reference.c"]


def test_clean_submission_passes_against_the_oracle(tmp_path):
    d = minimal_problem(tmp_path, oracle="llm_consensus", examples=EXAMPLES)
    (d / "submission.c").write_text((ADD / "submissions" / "add_clean.c").read_text())
    r, _, _ = graded(d, FakeLLM(tmp_path), tmp_path)
    by = {m["name"]: m["score"] for m in r["metrics"]}
    assert by["correctness"] == 1.0 and by["logical"] == 1.0


def test_oracle_that_fails_an_instructor_example_is_rejected(tmp_path):
    d = minimal_problem(tmp_path, oracle="llm_consensus", examples=EXAMPLES)
    llm = FakeLLM(tmp_path, programs={"simplest": INT_ADD, "efficient": INT_ADD, "careful": INT_ADD + " "})
    r, prep, store = graded(d, llm, tmp_path)
    assert r["asset_preparation"]["expected_outputs_from"] == "instructor_outputs"
    assert all(a.status == "rejected" for a in store.assets(prep.bundle_key) if a.kind == "oracle")
    assert any(f["type"] == "asset_generation_failed" and "oracle" in f["detail"] for f in r["flags"])
    # correctness falls back to the instructor's examples only
    c = next(m for m in r["metrics"] if m["name"] == "correctness")
    assert c["evidence"]["tests_total"] == 2


def test_disagreeing_oracle_candidates_only_supply_agreed_inputs(tmp_path):
    # both reproduce the two examples, but disagree on e.g. -2147483648 -1
    wrong = '#include <stdio.h>\nint main(void){long long a,b;if(scanf("%lld %lld",&a,&b)!=2)return 1;' \
            'if(a<-5)a=-a;printf("%lld\\n",a+b);return 0;}\n'
    d = minimal_problem(tmp_path, oracle="llm_consensus", examples=EXAMPLES, oracle_candidates=2)
    llm = FakeLLM(tmp_path, programs={"simplest": wrong, "efficient": GOOD_ADD, "careful": GOOD_ADD})
    r, prep, store = graded(d, llm, tmp_path)
    prep_sec = r["asset_preparation"]
    assert prep_sec["expected_outputs_from"] == "llm_consensus_inputs"
    assert prep_sec["oracle"]["mode"] == "partial" and prep_sec["oracle"]["disagreeing_inputs"] > 0
    assert any("disagree" in f["detail"] for f in prep_sec["failures"])
    # no program is trusted for unseen inputs: no generated reference, no random stress tests
    assert not [a for a in store.assets(prep.bundle_key) if a.kind == "reference"]
    assert any("random stress" in s and "trusted" in s for s in prep_sec["metrics"]["logical"]["skipped"])
    assert any("oracle candidates disagree" in s for s in prep_sec["metrics"]["logical"]["skipped"])
    logical = next(m for m in r["metrics"] if m["name"] == "logical")
    assert logical["status"] == "ok" and sum(f["total"] for f in logical["evidence"]["families"]) > 0


def test_oracle_needs_enough_instructor_examples(tmp_path):
    d = minimal_problem(tmp_path, oracle="llm_consensus", examples=EXAMPLES[:1], min_examples_for_oracle=2)
    r, _, _ = graded(d, FakeLLM(tmp_path), tmp_path)
    assert any("at least 2" in f["detail"] for f in r["asset_preparation"]["failures"])


def test_static_screen_blocks_dangerous_programs():
    assert screen_c_source(GOOD_ADD) == []
    for bad in ['#include <stdio.h>\nint main(){system("rm -rf /");}',
                '#include "/etc/passwd"\nint main(){}',
                '#include <unistd.h>\nint main(){fork();}',
                'int main(){__asm__("nop");}']:
        assert screen_c_source(bad), bad


# -- validation, retries, failures ---------------------------------------------------------
def test_invalid_spec_is_retried_with_feedback_then_accepted(tmp_path):
    bad = {"version": 1, "lines": [{"tokens": [{"name": "a", "type": "int", "min": 0, "max": 10}]}]}
    d = minimal_problem(tmp_path)
    llm = FakeLLM(tmp_path, spec=[bad, ADD_SPEC])
    prep, store = prepare(d, llm, tmp_path)
    events = [e for e in store.events(prep.bundle_key) if e["step"] == "input_spec"]
    assert [e["outcome"] for e in events] == ["rejected", "accepted"]
    assert "does not match" in events[0]["detail"]  # the sample "7 5" breaks the bad spec
    spec_calls = [c for c in llm.calls if "INPUT FORMAT" in c[0]["content"]]
    assert "validator rejected" in spec_calls[1][-1]["content"]


def test_generation_failure_is_reported_as_such_not_as_student_failure(tmp_path):
    bad = {"version": 1, "lines": [{"tokens": [{"name": "a", "type": "nope"}]}]}
    d = minimal_problem(tmp_path, max_retries=2)
    r, prep, store = graded(d, FakeLLM(tmp_path, spec=bad), tmp_path)
    by = {m["name"]: m for m in r["metrics"]}
    assert by["complexity"]["status"] == "not_performed"  # not "skipped" (student's fault)
    assert any(f["type"] == "asset_generation_failed" and f["detail"].startswith("input_spec")
               for f in r["flags"])
    assert [e["outcome"] for e in store.events(prep.bundle_key) if e["step"] == "input_spec"] == \
        ["rejected", "rejected"]


def test_llm_unreachable_falls_back_to_instructor_spec(tmp_path):
    class Down(FakeLLM):
        def chat_json(self, **kw):
            raise LLMUnavailable("http://127.0.0.1:11434/api/chat: connection refused")

    d = minimal_problem(tmp_path, input_spec=ADD_SPEC)
    r, prep, store = graded(d, Down(tmp_path), tmp_path)
    by = {m["name"]: m for m in r["metrics"]}
    assert by["complexity"]["status"] == "ok"  # deterministic generator from the instructor's spec
    assert by["memory_safety"]["evidence"]["inputs_checked"] > 1  # deterministic boundary inputs
    assert any(f["step"] == "case_families" for f in r["asset_preparation"]["failures"])


def test_bundle_built_while_llm_was_down_is_regenerated_later(tmp_path):
    class Down(FakeLLM):
        def chat_json(self, **kw):
            raise LLMUnavailable("connection refused")

    d = minimal_problem(tmp_path)
    down, store = prepare(d, Down(tmp_path), tmp_path)
    assert down.overlay.not_performed.get("complexity")
    assert any(a.status == "unavailable" for a in store.assets(down.bundle_key))
    offline = Down(tmp_path)
    offline.offline = True
    assert prepare(d, offline, tmp_path)[0].cache_hit  # offline: reuse what exists
    up, _ = prepare(d, FakeLLM(tmp_path), tmp_path)
    assert not up.cache_hit and "complexity" not in up.overlay.not_performed


def test_invalid_generated_inputs_are_rejected_and_recorded(tmp_path):
    d = minimal_problem(tmp_path)
    prep, store = prepare(d, FakeLLM(tmp_path), tmp_path)
    rejected = {a.content.strip(): a.detail for a in store.assets(prep.bundle_key, "rejected")
                if a.kind == "test_input"}
    assert "1 2 3" in rejected and "extra value" in rejected["1 2 3"]
    assert "99999999999 1" in rejected and "outside" in rejected["99999999999 1"]
    valid = [a.content for a in store.assets(prep.bundle_key, "validated") if a.kind == "test_input"]
    assert "2147483647 1\n" in valid  # newline normalised, then accepted


def test_complexity_sizes_outside_the_spec_are_not_performed(tmp_path):
    spec = {"version": 1, "lines": [
        {"tokens": [{"name": "n", "type": "int", "min": 1, "max": 50, "size": True}]},
        {"tokens": [{"name": "a", "type": "int", "min": 0, "max": 9, "count": "n"}]}]}
    metrics = [{"name": "complexity", "analyzer": "complexity_estimator", "weight": 1.0,
                "params": {"required": "O(n)", "sizes": [10, 100, 1000]}}]
    d = minimal_problem(tmp_path, metrics=metrics, input_spec=spec)
    rules = json.loads((d / "rules.json").read_text())
    rules["sample_input"] = "3\n1 2 3\n"
    (d / "rules.json").write_text(json.dumps(rules))
    r, _, _ = graded(d, FakeLLM(tmp_path), tmp_path)
    assert r["metrics"][0]["status"] == "not_performed" and "outside" in r["metrics"][0]["feedback"]
    assert r["grade"] == 0.0


# -- caching ------------------------------------------------------------------------------
def test_validated_bundle_is_reused_and_invalidated_when_inputs_change(tmp_path):
    d = minimal_problem(tmp_path)
    llm = FakeLLM(tmp_path)
    first, store = prepare(d, llm, tmp_path)
    n_calls = len(llm.calls)
    again, _ = prepare(d, llm, tmp_path)
    assert again.cache_hit and again.bundle_key == first.bundle_key and len(llm.calls) == n_calls

    rules = json.loads((d / "rules.json").read_text())
    rules["asset_generation"]["description"] += "\nExtra note."
    (d / "rules.json").write_text(json.dumps(rules))
    changed, _ = prepare(d, llm, tmp_path)
    assert not changed.cache_hit and changed.bundle_key != first.bundle_key

    (d / "reference.c").write_text(GOOD_ADD)  # an instructor asset appears
    with_ref, _ = prepare(d, llm, tmp_path)
    assert with_ref.bundle_key != changed.bundle_key
    assert with_ref.overlay.report["expected_outputs_from"] == "instructor_reference"


def test_model_digest_change_regenerates(tmp_path):
    d = minimal_problem(tmp_path)
    one, _ = prepare(d, FakeLLM(tmp_path, digest="sha256:aaa"), tmp_path)
    same, _ = prepare(d, FakeLLM(tmp_path, digest="sha256:aaa"), tmp_path)
    other, _ = prepare(d, FakeLLM(tmp_path, digest="sha256:bbb"), tmp_path)
    offline, _ = prepare(d, FakeLLM(tmp_path, digest=None), tmp_path)  # cannot tell: reuse
    assert same.cache_hit and not other.cache_hit and offline.cache_hit


def test_grades_are_reproducible_across_cache_hits(tmp_path):
    metrics = [dict(m) for m in METRICS if m["name"] != "complexity"]  # CPU time is noisy on macOS
    metrics[0] = {**metrics[0], "weight": 0.4}
    d = minimal_problem(tmp_path, metrics=metrics, oracle="llm_consensus", examples=EXAMPLES)
    r1, _, _ = graded(d, FakeLLM(tmp_path), tmp_path)
    r2, prep, _ = graded(d, FakeLLM(tmp_path), tmp_path)
    assert prep.cache_hit and r1["fingerprint"] == r2["fingerprint"]


def test_tampered_materialised_assets_are_rebuilt_from_the_database(tmp_path):
    d = minimal_problem(tmp_path)
    prep, store = prepare(d, FakeLLM(tmp_path), tmp_path)
    (prep.bundle_dir / "inputs" / "replay_inputs.json").write_text('{"x": ["evil"]}')
    (prep.bundle_dir / "extra.json").write_text("{}")
    again, _ = prepare(d, FakeLLM(tmp_path), tmp_path)
    assert not (again.bundle_dir / "extra.json").exists()
    assert "evil" not in (again.bundle_dir / "inputs" / "replay_inputs.json").read_text()


def test_asset_paths_cannot_escape_the_bundle(tmp_path):
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(ValueError):
            _safe_path(tmp_path, bad)
    with pytest.raises(ValueError):
        materialize([AssetRecord("inputs", "x", "validated", "llm", "", "x", "../escape")], tmp_path / "b")


# -- inventory + spec unit tests -------------------------------------------------------------
def test_find_missing_reports_each_analyzers_needs(tmp_path):
    d = minimal_problem(tmp_path)
    missing = find_missing(json.loads((d / "rules.json").read_text()), d)
    assert missing["<rules>"] == ["reference solution reference.c"]
    assert "test inputs" in missing["correctness"]
    assert "corner cases" in missing["logical"] and "input generator" in missing["logical"]
    assert missing["complexity"] == ["input generator"]
    assert "syntax" not in missing and "modularity" not in missing
    assert find_missing(json.loads((ADD / "rules.json").read_text()), ADD) == {}


def test_input_spec_validates_generates_and_finds_boundaries():
    spec = InputSpec({"version": 1, "lines": [
        {"tokens": [{"name": "n", "type": "int", "min": 1, "max": 100, "size": True},
                    {"name": "k", "type": "int", "min": 1, "max": "n"}]},
        {"tokens": [{"name": "a", "type": "int", "min": -5, "max": 5, "count": "n", "sorted": "asc"}]},
        {"repeat": "k", "tokens": [{"name": "w", "type": "word", "charset": "lower", "max_len": 4}]}]})
    assert spec.validate("3 2\n-1 0 4\nab\nc\n") == []
    assert "not sorted" in spec.validate("3 2\n4 0 -1\nab\nc\n")[0]
    assert "outside" in spec.validate("3 4\n-1 0 4\nab\nc\nd\ne\n")[0]  # k <= n
    assert "ends early" in spec.validate("3 2\n-1 0 4\nab\n")[0]
    for size in (1, 7, 100):
        text = spec.generate(size, random.Random(size))
        assert spec.validate(text) == [] and text == spec.generate(size, random.Random(size))
    with pytest.raises(SpecError):
        spec.generate(101, random.Random(0))
    fams = spec.boundary_cases(random.Random(0))
    assert "minimum_size" in fams and all(spec.validate(t[0]) == [] for t in fams.values())


def test_spec_checker_rejects_what_it_cannot_honour():
    assert check_spec({"version": 1, "lines": [{"tokens": [{"name": "x", "type": "int", "min": 0, "max": 1}]}],
                       "unsupported_constraints": ["graph is connected"]})
    assert check_spec({"version": 1, "lines": [{"tokens": [
        {"name": "a", "type": "int", "min": 0, "max": 9, "count": "n"}]}]})  # n not defined earlier
    assert check_spec({"version": 1, "lines": [{"tokens": [
        {"name": "x", "type": "int", "min": 5, "max": 1}]}]})
    assert check_spec({"lines": []})


# -- CLI + database ---------------------------------------------------------------------------
def test_cli_grade_generate_assets_offline_records_in_sqlite(tmp_path):
    d = minimal_problem(tmp_path, input_spec=ADD_SPEC, examples=EXAMPLES)
    cache, out = tmp_path / "cache", tmp_path / "reports"
    code = cli.main(["grade", str(d / "submission.c"), "--rules", str(d / "rules.json"), "--out", str(out),
                     "--cache", str(cache), "--offline", "--generate-assets", "--quiet"])
    assert code == 0
    report = json.loads((out / "submission.json").read_text())
    assert report["asset_preparation"]["expected_outputs_from"] == "instructor_outputs"
    assert "Test assets (generated workflow)" in (out / "submission.txt").read_text()
    store = AssetStore(cache / cli.DB_NAME)
    rows = store.grades()
    assert len(rows) == 1 and rows[0]["fingerprint"] == report["fingerprint"]
    assert rows[0]["bundle_key"] == report["asset_preparation"]["bundle_key"]

    assert cli.main(["prepare", "--rules", str(d / "rules.json"), "--cache", str(cache), "--offline"]) == 0


def test_cli_explicit_workflow_unchanged_and_recorded(add_problem, tmp_path):
    out = tmp_path / "reports"
    code = cli.main(["grade", str(add_problem / "submissions" / "add_clean.c"), "--rules",
                     str(add_problem / "rules.json"), "--out", str(out), "--cache", str(tmp_path / "c"),
                     "--offline", "--quiet"])
    assert code == 0
    report = json.loads((out / "add_clean.json").read_text())
    assert "asset_preparation" not in report
    assert AssetStore(tmp_path / "c" / cli.DB_NAME).grades()[0]["bundle_key"] is None
    cli.main(["grade", str(add_problem / "submissions" / "add_clean.c"), "--rules",
              str(add_problem / "rules.json"), "--out", str(out), "--cache", str(tmp_path / "d"),
              "--offline", "--quiet", "--no-db"])
    assert not (tmp_path / "d" / cli.DB_NAME).exists()
