"""Web UI: the JSON API over a real local server (needs a C compiler; no LLM)."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from autograder.web.server import App, check_uploaded_rules, make_server
from tests.conftest import ADD
from tests.test_assets import ADD_SPEC, EXAMPLES


@pytest.fixture
def server(tmp_path, add_problem):
    app = App(add_problem.parent, tmp_path / "cache", offline_default=True)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_serves_the_page_and_lists_rules_files(server):
    with urllib.request.urlopen(server + "/") as r:
        assert r.status == 200 and b"<title>AutoGrader</title>" in r.read()
    status, data = call(server, "/api/problems")
    assert status == 200 and "add_two_integers/rules.json" in data["rules"]


def test_grade_with_folder_rules_is_recorded_in_history(server):
    source = (ADD / "submissions" / "add.c").read_text()
    status, data = call(server, "/api/grade", {"submission": {"name": "add.c", "source": source},
                                               "rules": {"path": "add_two_integers/rules.json"}, "offline": True})
    assert status == 200, data
    assert data["report"]["submission"] == "add.c" and "Grading report for add.c" in data["text"]
    assert "asset_preparation" not in data["report"]  # explicit workflow
    _, hist = call(server, "/api/grades")
    assert [g["id"] for g in hist["grades"]] == [data["id"]]
    _, one = call(server, f"/api/grades/{data['id']}")
    assert one["report"]["fingerprint"] == data["report"]["fingerprint"]


def test_uploaded_rules_with_generated_assets_and_bundle_inspection(server):
    rules = {"problem": "add_two_integers", "time_limit_s": 1, "memory_limit_mb": 64, "sample_input": "7 5\n",
             "include_usefulness_limitations": False,
             "asset_generation": {"input_spec": ADD_SPEC, "examples": EXAMPLES},
             "metrics": [{"name": "correctness", "analyzer": "io_tests", "weight": 0.5, "params": {"testset_dir": "tests/"}},
                         {"name": "memory_safety", "analyzer": "sanitizer_run", "weight": 0.5, "params": {"valgrind": False}}]}
    body = {"submission": {"name": "s.c", "source": (ADD / "submissions" / "add_clean.c").read_text()},
            "rules": {"json": json.dumps(rules)}, "generate_assets": True, "offline": True}
    status, data = call(server, "/api/grade", body)
    assert status == 200, data
    prep = data["report"]["asset_preparation"]
    # only the 2 instructor examples have trusted outputs; generated inputs are skipped
    assert prep["metrics"]["correctness"]["status"] == "partial"
    assert data["report"]["metrics"][0]["evidence"]["tests_total"] == 2
    _, bundles = call(server, "/api/bundles")
    assert [b["bundle_key"] for b in bundles["bundles"]] == [prep["bundle_key"]]
    _, detail = call(server, f"/api/bundles/{prep['bundle_key']}")
    assert any(a["kind"] == "input_spec" and a["status"] == "validated" for a in detail["assets"])


def test_explicit_upload_without_assets_explains_how_to_fix(server):
    rules = {"problem": "p", "reference": "reference.c",
             "metrics": [{"name": "c", "analyzer": "io_tests", "weight": 1.0, "params": {"testset_dir": "tests/"}}]}
    status, data = call(server, "/api/grade", {"submission": {"name": "a.c", "source": "int main(){return 0;}"},
                                               "rules": {"json": json.dumps(rules)}})
    assert status == 400 and any("generate missing assets" in p for p in data["problems"])


@pytest.mark.parametrize("body,needle", [
    ({"submission": {"name": "../x.c", "source": "int main(){}"}, "rules": {"path": "add_two_integers/rules.json"}}, "name"),
    ({"submission": {"name": "a.c", "source": "int main(){}"}, "rules": {"path": "../../etc/passwd"}}, "not found"),
    ({"submission": {"name": "a.c", "source": ""}, "rules": {"path": "add_two_integers/rules.json"}}, "empty"),
])
def test_rejects_unsafe_requests(server, body, needle):
    status, data = call(server, "/api/grade", body)
    assert status == 400 and needle in data["error"]


def test_uploaded_rules_cannot_load_plugins_or_escape():
    assert check_uploaded_rules({"plugins": ["evil.py"], "metrics": []})
    assert check_uploaded_rules({"reference": "/etc/passwd", "metrics": []})
    assert check_uploaded_rules({"metrics": [{"params": {"testset_dir": "../../secret"}}]})
    assert check_uploaded_rules({"reference": "reference.c", "metrics": [{"params": {"testset_dir": "tests/"}}]}) == []


def test_upload_identical_to_a_corpus_file_is_not_compared_with_itself(server):
    source = (ADD / "submissions" / "add_clean.c").read_text()
    body = {"submission": {"name": "add_clean.c", "source": source},
            "rules": {"path": "add_two_integers/rules.json"}, "offline": True}
    matches = lambda r: {f.get("match") for f in r["report"]["flags"] if f["type"] == "suspected_plagiarism"}
    _, same = call(server, "/api/grade", body)
    assert "submissions/add_clean.c" not in matches(same)
    assert "submissions/add_copied.c" in matches(same)  # the real copy is still caught
    body["submission"]["name"] = "renamed.c"  # a copy under another name is still a copy
    _, other = call(server, "/api/grade", body)
    assert "submissions/add_clean.c" in matches(other)
