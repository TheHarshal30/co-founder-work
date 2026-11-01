"""Automatic generation from submission.c + a bare rules.json (no asset_generation
block, no statement, no tests, reference, generator or corpus), plus the
pieces it relies on: oracle validation, corner-case execution, complexity on a
generated generator, originality thresholds, persistence, the TXT report and
LLM connectivity. LLM mocked; needs a C compiler (run in Docker on macOS)."""

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from autograder import cli, llm as llm_mod
from autograder.analyzers import REGISTRY
from autograder.analyzers.similarity_flag import SimilarityFlag, _verdict, fingerprints
from autograder.assets import AssetStore, prepare_assets
from autograder.coordinator import grade
from autograder.llm import LLMClient, LLMUnavailable
from autograder.report import render_text
from autograder.rules import load_rules
from tests.conftest import ADD, ROOT
from tests.test_assets import BRUTE_ADD, CAREFUL_ADD, GOOD_ADD, METRICS, FakeLLM

BARE = {"problem": "add_two_integers", "time_limit_s": 1, "memory_limit_mb": 64,
        "sample_input": "7 5\n", "sample_output": "12\n", "include_usefulness_limitations": False}
WRONG_SAMPLE = GOOD_ADD.replace("a+b", "a+b+1")
CRASH_ON_ZERO = GOOD_ADD.replace('printf(', 'if(a==0){int*p=0;*p=1;}printf(')


def bare_problem(tmp_path, metrics=METRICS, submission="add.c", **top):
    """ONLY rules.json + submission.c: no asset_generation, no statement file."""
    d = tmp_path / "bare"
    d.mkdir()
    shutil.copy(ADD / "submissions" / submission, d / "submission.c")
    rules = {**BARE, **top, "metrics": metrics}
    rules = {k: v for k, v in rules.items() if v is not None}
    (d / "rules.json").write_text(json.dumps(rules, indent=2))
    return d


def run(d, llm, tmp_path, **kw):
    store = AssetStore(tmp_path / "cache" / "autograder.sqlite3")
    prep = prepare_assets(d / "rules.json", llm, store, tmp_path / "cache", d / "submission.c", **kw)
    rules = load_rules(d / "rules.json", REGISTRY, prep.overlay)
    return grade(d / "submission.c", rules, llm), prep, store


def by_name(report):
    return {m["name"]: m for m in report["metrics"]}


# -- 1. automatic generation without an asset_generation block ---------------------------
def test_bare_rules_infer_generate_validate_and_run_every_metric(tmp_path):
    d = bare_problem(tmp_path)
    llm = FakeLLM(tmp_path)
    r, prep, store = run(d, llm, tmp_path)
    m = by_name(r)

    # the problem was inferred from the title and the published sample
    prompts = [c[1]["content"] for c in llm.calls]
    assert all("Problem title: add two integers" in p for p in prompts)
    assert any("Example 1 expected output:\n12" in p for p in prompts)

    sec = r["asset_preparation"]
    assert sec["expected_outputs_from"] == "llm_consensus_oracle"
    assert sec["oracle"]["mode"] == "full" and sec["oracle"]["anchored_on_examples"] == 1
    assert not sec["failures"]
    # nothing is "not performed": every metric ran on validated generated assets
    assert [n for n, x in m.items() if x["status"] != "ok"] == []
    assert m["correctness"]["evidence"]["tests_total"] > 1 and m["correctness"]["score"] < 1
    assert m["complexity"]["evidence"]["measurements"] and m["memory_safety"]["evidence"]["inputs_checked"] > 1
    assert r["grade_complete"] is True


def test_generated_corner_cases_are_executed_with_trusted_expected_outputs(tmp_path):
    d = bare_problem(tmp_path)
    r, prep, _ = run(d, FakeLLM(tmp_path), tmp_path)
    logical = by_name(r)["logical"]
    fams = {f["family"]: f for f in logical["evidence"]["families"]}
    assert logical["status"] == "ok" and logical["score"] < 1
    assert "random_stress" in fams and fams["random_stress"]["total"] == 6
    assert fams["all_maximum_values"]["passed"] == 0  # add.c: int overflow
    cases = json.loads((prep.bundle_dir / "edge_cases" / "logical.json").read_text())
    assert all("expected" in p for probes in cases.values() for p in probes)
    assert cases["all_maximum_values"] == [{"input": "2147483647 2147483647\n", "expected": "4294967294\n"}]
    assert logical["evidence"]["failing"]  # shown to the student as counterexamples


def test_complexity_runs_on_a_generated_generator_with_increasing_sizes(tmp_path):
    spec = {"version": 1, "lines": [
        {"tokens": [{"name": "n", "type": "int", "min": 1, "max": 200000, "size": True}]},
        {"tokens": [{"name": "a", "type": "int", "min": -1000, "max": 1000, "count": "n"}]}]}
    metrics = [{"name": "complexity", "analyzer": "complexity_estimator", "weight": 1.0,
                "params": {"required": "O(n)", "sizes": [100, 200, 400, 800, 10**9], "method": "cpu_time",
                           "repeats": 1}}]
    d = bare_problem(tmp_path, metrics=metrics, problem="sum_array", sample_input="3\n1 2 3\n", sample_output=None)
    (d / "submission.c").write_text('#include <stdio.h>\nint main(void){int n,x;long long s=0;scanf("%d",&n);'
                                    'for(int i=0;i<n;i++){scanf("%d",&x);s+=x;}printf("%lld\\n",s);return 0;}\n')
    r, prep, _ = run(d, FakeLLM(tmp_path, spec=spec), tmp_path)
    c = by_name(r)["complexity"]
    assert c["status"] == "ok", c["feedback"]
    assert [p["n"] for p in c["evidence"]["measurements"]] == [100, 200, 400, 800]  # 10^9 > max n: dropped
    assert "outside" in r["asset_preparation"]["metrics"]["complexity"]["detail"]
    gen = (prep.bundle_dir / "generators" / "gen.py").read_text()
    assert "model-written code" in gen and '"size": true' in gen


# -- 2. oracle validation ---------------------------------------------------------------
def test_oracle_candidate_that_fails_the_sample_is_rejected_with_the_reason(tmp_path):
    d = bare_problem(tmp_path)
    llm = FakeLLM(tmp_path, programs={"simplest": BRUTE_ADD, "efficient": WRONG_SAMPLE, "careful": CAREFUL_ADD})
    r, prep, store = run(d, llm, tmp_path)
    cands = {c["label"]: c for c in r["asset_preparation"]["oracle"]["candidates"]}
    assert cands["efficient"]["status"] == "rejected"
    assert "expected '12', printed '13'" in cands["efficient"]["detail"]
    assert r["asset_preparation"]["oracle"]["mode"] == "full"  # the two others still anchor + agree
    assert (prep.bundle_dir / "oracles" / "efficient.c").exists()  # kept for review, never used


def test_oracle_candidate_crashing_on_a_valid_input_is_dropped_not_fatal(tmp_path):
    d = bare_problem(tmp_path)
    llm = FakeLLM(tmp_path, programs={"simplest": BRUTE_ADD, "efficient": CRASH_ON_ZERO, "careful": CAREFUL_ADD})
    r, _, _ = run(d, llm, tmp_path)
    cands = {c["label"]: c for c in r["asset_preparation"]["oracle"]["candidates"]}
    assert "crashed" in cands["efficient"]["detail"]
    assert r["asset_preparation"]["oracle"]["mode"] == "full"


def test_unanchored_oracle_needs_every_candidate_and_is_flagged(tmp_path):
    d = bare_problem(tmp_path, sample_output=None)
    r, _, _ = run(d, FakeLLM(tmp_path), tmp_path)
    sec = r["asset_preparation"]
    assert sec["oracle"]["mode"] == "full" and sec["oracle"]["required_agreeing"] == 3
    assert any(f["type"] == "llm_oracle_unanchored" for f in r["flags"])

    second = tmp_path / "second"  # separate cache: same rules, different candidates
    second.mkdir()
    d2 = bare_problem(second, sample_output=None)
    llm = FakeLLM(second, programs={"simplest": BRUTE_ADD, "efficient": GOOD_ADD, "careful": GOOD_ADD})
    r2, _, _ = run(d2, llm, second)
    logical = by_name(r2)["logical"]
    assert logical["status"] == "not_performed"
    assert "oracle was rejected" in logical["feedback"] and "3 independent ones are required" in logical["feedback"]


def test_unreachable_llm_gives_a_precise_not_performed_reason(tmp_path):
    class Down(FakeLLM):
        def chat_json(self, **kw):
            raise LLMUnavailable("Ollama is not reachable at http://host.docker.internal:11434")

    d = bare_problem(tmp_path)
    r, _, _ = run(d, Down(tmp_path), tmp_path)
    m = by_name(r)
    for name in ("logical", "complexity"):
        assert m[name]["status"] == "not_performed"
        assert "not reachable at http://host.docker.internal:11434" in m[name]["feedback"]
    assert m["correctness"]["evidence"]["tests_total"] == 1  # the instructor's sample still runs


def test_input_format_hint_from_the_submission_only_when_no_instructor_input(tmp_path):
    d = bare_problem(tmp_path, sample_input=None, sample_output=None)
    llm = FakeLLM(tmp_path)
    r, prep, _ = run(d, llm, tmp_path)
    spec_prompt = next(c[1]["content"] for c in llm.calls if "INPUT FORMAT" in c[0]["content"])
    assert 'scanf("%d %d",&a,&b);' in spec_prompt
    others = [c[1]["content"] for c in llm.calls if "INPUT FORMAT" not in c[0]["content"]]
    assert others and not any("scanf" in p for p in others)  # tests and oracles never see it
    assert any(f["type"] == "input_format_from_submission" for f in r["flags"])
    assert "NOT checked" in r["asset_preparation"]["spec_basis"]


# -- 3. persistence -------------------------------------------------------------------------
def test_generated_assets_are_persisted_by_kind_and_reused(tmp_path):
    d = bare_problem(tmp_path)
    llm = FakeLLM(tmp_path)
    r, prep, store = run(d, llm, tmp_path)
    gid = r["asset_preparation"]["generation_id"]
    assert prep.bundle_dir == tmp_path / "cache" / "generated" / "add_two_integers" / gid
    files = {p.relative_to(prep.bundle_dir).as_posix() for p in prep.bundle_dir.rglob("*") if p.is_file()}
    for expected in ("manifest.json", "validation/report.json", "spec/input_spec.json", "generators/gen.py",
                     "reference/reference.c", "oracles/brute_force.c", "oracles/efficient.c", "oracles/careful.c",
                     "edge_cases/logical.json", "inputs/replay_inputs.json", "known/sources.json"):
        assert expected in files, expected
    assert any(f.startswith("tests/correctness/") and f.endswith(".out") for f in files)
    report = json.loads((prep.bundle_dir / "validation" / "report.json").read_text())
    assert {a["status"] for a in report["assets"]} >= {"validated"}
    assert (prep.bundle_dir / "reference" / "reference.c").read_text() == GOOD_ADD  # the efficient candidate
    assert not (d / "tests").exists() and sorted(p.name for p in d.iterdir()) == ["rules.json", "submission.c"]

    n_calls = len(llm.calls)
    again, prep2, _ = run(d, llm, tmp_path)
    assert prep2.cache_hit and prep2.bundle_dir == prep.bundle_dir and len(llm.calls) == n_calls
    assert again["fingerprint"] == r["fingerprint"] or by_name(again)["complexity"]["evidence"]["method"] == "cpu_time"

    rules = json.loads((d / "rules.json").read_text())
    rules["time_limit_s"] = 2  # the problem changed: new generation
    (d / "rules.json").write_text(json.dumps(rules))
    _, prep3, _ = run(d, llm, tmp_path)
    assert not prep3.cache_hit and prep3.bundle_dir != prep.bundle_dir


def test_llm_model_override_is_part_of_the_cache_key(tmp_path, monkeypatch):
    d = bare_problem(tmp_path)
    store = AssetStore(tmp_path / "db.sqlite3")
    a = prepare_assets(d / "rules.json", FakeLLM(tmp_path), store, tmp_path / "c")
    monkeypatch.setenv("AUTOGRADER_LLM_MODEL", "llama3.1:8b")
    b = prepare_assets(d / "rules.json", FakeLLM(tmp_path), store, tmp_path / "c")
    c = prepare_assets(d / "rules.json", FakeLLM(tmp_path), store, tmp_path / "c", overrides={"model": "x:1b"})
    assert len({a.bundle_key, b.bundle_key, c.bundle_key}) == 3
    assert store.get_bundle(b.bundle_key)["model_name"] == "llama3.1:8b"
    assert store.get_bundle(c.bundle_key)["model_name"] == "x:1b"


# -- 4. originality thresholds ------------------------------------------------------------------
def _similarity(tmp_path, mine: str, others: dict, reference=None, **params):
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    for name, src in others.items():
        (corpus / name).write_text(src)
    sub = tmp_path / "mine.c"
    sub.write_text(mine)
    ref = None
    if reference:
        ref = tmp_path / "reference.c"
        ref.write_text(reference)
    rules = SimpleNamespace(reference=ref, base_dir=tmp_path, resolve=lambda rel: tmp_path / rel)
    ctx = SimpleNamespace(rules=rules, source=mine, submission=sub)
    return SimilarityFlag().run(ctx, {"corpus_dir": "corpus", **params})


TINY_A = '#include <stdio.h>\nint main(){int a,b;scanf("%d %d",&a,&b);printf("%d\\n",a+b);return 0;}\n'
TINY_B = '#include <stdio.h>\nint main(){int x,y;scanf("%d%d",&x,&y);printf("%d",x+y);return 0;}\n'


def test_fewer_than_min_fingerprints_is_suspect_never_flagged(tmp_path):
    # without a reference to subtract, a one-line program still has ~10 fingerprints:
    # the shape every solution shares, which must not count as evidence
    assert len(fingerprints(TINY_A)) >= 8
    r = _similarity(tmp_path, TINY_A, {"other.c": TINY_B})
    assert r.evidence["fingerprints"] < 8 and r.evidence["ignored_boilerplate_fingerprints"] > 0
    assert r.evidence["top_matches"][0]["raw_similarity"] >= 0.85  # identical after renaming...
    assert r.score == 1.0 and r.evidence["verdict"] == "suspect"  # ...but too little code to accuse anyone
    assert [f["type"] for f in r.flags] == ["similarity_suspect"]
    assert "SUSPECT" in r.feedback and "FLAGGED" not in r.feedback


def test_small_add_programs_do_not_accuse_each_other(tmp_path):
    clean = (ADD / "submissions" / "add_clean.c").read_text()
    llm_known = {p.name: p.read_text() for p in (ADD / "known").glob("*.c")}
    r = _similarity(tmp_path, (ADD / "submissions" / "add.c").read_text(), {**llm_known, "add_clean.c": clean},
                    reference=(ADD / "reference.c").read_text())
    assert not any(f["type"] in ("suspected_plagiarism", "suspected_external_copy") for f in r.flags)
    assert r.score == 1.0


def test_a_real_copy_with_enough_evidence_is_still_flagged(tmp_path):
    clean = (ADD / "submissions" / "add_clean.c").read_text()
    copied = (ADD / "submissions" / "add_copied.c").read_text()
    r = _similarity(tmp_path, copied, {"add_clean.c": clean}, reference=(ADD / "reference.c").read_text())
    assert [f["type"] for f in r.flags] == ["suspected_plagiarism"] and r.score == 0.0
    top = r.evidence["top_matches"][0]
    assert top["shared_fingerprints"] >= 8 and top["verdict"] == "flagged"
    # the same pair with a stricter shared-evidence bar is only SUSPECT
    strict = _similarity(tmp_path, copied, {"add_clean.c": clean}, reference=(ADD / "reference.c").read_text(),
                         min_shared_fingerprints=50)
    assert [f["type"] for f in strict.flags] == ["similarity_suspect"] and strict.score == 1.0


def test_min_fingerprint_boundary():
    m = {"similarity": 0.9, "raw_similarity": 0.9, "fingerprints": 8, "shared_fingerprints": 8}
    assert _verdict(m, 8, 0.85, 8, 8) == "flagged"
    assert _verdict(m, 7, 0.85, 8, 8) == "suspect"  # mine below the minimum
    assert _verdict({**m, "fingerprints": 7}, 8, 0.85, 8, 8) == "suspect"  # theirs below
    assert _verdict({**m, "shared_fingerprints": 7}, 8, 0.85, 8, 8) == "suspect"
    assert _verdict({**m, "similarity": 0.84}, 8, 0.85, 8, 8) == "below_threshold"
    assert _verdict({**m, "similarity": 0.2, "raw_similarity": 1.0}, 3, 0.85, 8, 8) == "suspect"
    assert _verdict({**m, "similarity": 0.2, "raw_similarity": 1.0}, 8, 0.85, 8, 8) == "below_threshold"


def test_fingerprint_algorithm_is_unchanged():
    # pinned: a change here changes every originality verdict ever recorded
    assert sorted(fingerprints((ADD / "submissions" / "add_clean.c").read_text()))[:3] == \
        sorted(fingerprints((ADD / "submissions" / "add_copied.c").read_text()))[:3]
    assert len(fingerprints((ADD / "submissions" / "add_clean.c").read_text())) == 28
    assert len(fingerprints((ADD / "reference.c").read_text())) == 13


# -- 5. text report ---------------------------------------------------------------------------
def test_text_report_has_scores_verdicts_findings_assets_and_reasons(tmp_path):
    class Down(FakeLLM):
        def chat_json(self, **kw):
            raise LLMUnavailable("Ollama is not reachable at http://127.0.0.1:11434")

    d = bare_problem(tmp_path)
    r, _, _ = run(d, Down(tmp_path), tmp_path)
    text = render_text(r)
    for needle in ("Overall grade:", "Status", "NOT PERFORMED", "--- Analyzer findings ---",
                   "--- Not performed / skipped, and why ---", "--- Test assets (generated workflow) ---",
                   "Stored in: <cache>/generated/add_two_integers/", "not reachable at http://127.0.0.1:11434",
                   "generation failure (not the student's fault)", "syntax: 1 warning(s)"):
        assert needle in text, needle


# -- 6. LLM connectivity ------------------------------------------------------------------------
def test_ollama_defaults_to_the_host_from_docker(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(llm_mod, "in_container", lambda: True)
    assert llm_mod._ollama_base() == "http://host.docker.internal:11434"
    monkeypatch.setattr(llm_mod, "in_container", lambda: False)
    assert llm_mod._ollama_base() == "http://127.0.0.1:11434"
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11500")
    assert llm_mod._ollama_base() == "http://127.0.0.1:11500"
    script = (ROOT / "grade.sh").read_text()
    assert "--add-host=host.docker.internal:host-gateway" in script
    assert "OLLAMA_HOST=\"${AUTOGRADER_OLLAMA_HOST:-http://host.docker.internal:11434}\"" in script
    assert "host.docker.internal:host-gateway" in (ROOT / "grade.ps1").read_text()


class FakeOllama(BaseHTTPRequestHandler):
    models = ["qwen2.5:7b"]

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"models": [{"name": m, "digest": "sha256:abc"} for m in self.models]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if req["model"] not in self.models:
            return self._send(404, {"error": f"model '{req['model']}' not found, try pulling it first"})
        self._send(200, {"message": {"content": json.dumps({"code": "int main(){return 0;}"})}})


@pytest.fixture
def ollama(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{srv.server_address[1]}")
    yield srv
    srv.shutdown()
    srv.server_close()


def test_ollama_protocol_check_and_model_not_pulled(ollama, tmp_path):
    client = LLMClient(tmp_path / "llm")
    assert client.check("ollama", "qwen2.5:7b")["ok"] is True
    missing = client.check("ollama", "llama3.1:8b")
    assert missing["ok"] is False and "ollama pull llama3.1:8b" in missing["error"]
    ans = client.chat_json(provider="ollama", model="qwen2.5:7b", messages=[{"role": "user", "content": "x"}])
    assert ans.data == {"code": "int main(){return 0;}"} and ans.model_digest == "sha256:abc"
    with pytest.raises(LLMUnavailable, match="ollama pull llama3.1:8b"):
        client.chat_json(provider="ollama", model="llama3.1:8b", messages=[{"role": "user", "content": "x"}])


def test_unreachable_ollama_error_says_what_to_do(tmp_path, monkeypatch, capsys):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{port}")
    client = LLMClient(tmp_path / "llm")
    with pytest.raises(LLMUnavailable, match=r"not reachable at http://127\.0\.0\.1:\d+.*ollama serve"):
        client.chat_json(provider="ollama", model="m", messages=[{"role": "user", "content": "x"}])
    assert "not reachable" in client.check("ollama", "m")["error"]
    assert cli.main(["llm-check", "--cache", str(tmp_path / "c"), "--llm-model", "m"]) == 1
    assert "NOT AVAILABLE" in capsys.readouterr().out


def test_cli_llm_check_ok_and_override(ollama, tmp_path, capsys):
    assert cli.main(["llm-check", "--cache", str(tmp_path / "c")]) == 0
    assert "OK: ollama model 'qwen2.5:7b'" in capsys.readouterr().out
    assert cli.main(["llm-check", "--cache", str(tmp_path / "c"), "--llm-model", "nope:1b"]) == 1
