"""Local web UI: grade a submission, inspect generated assets, browse past grades.

    autograder serve [--port 8000] [--host 127.0.0.1]

Standard library only (``http.server``), one static page plus a JSON API over
the same functions the CLI uses:

    GET  /api/problems          rules files under the working folder
    POST /api/grade             grade one submission (explicit or generated assets)
    GET  /api/grades            past grades (SQLite)
    GET  /api/grades/<id>       one stored report (+ its text rendering)
    GET  /api/grades/<id>/report.txt   the text report as a download
    GET  /api/bundles           generated asset bundles
    GET  /api/bundles/<key>     one bundle: every asset (incl. rejected), where
                                its files are stored, LLM events
    GET  /api/llm               is the asset-generation LLM reachable?

It binds to 127.0.0.1 by default: the grader compiles and runs uploaded C code
(inside the usual sandbox limits), so it is meant for one instructor's machine,
not the open network. Uploaded rules files are restricted further: no plugins
(they are Python) and no absolute or ``..`` paths (they could point the grader
at arbitrary files on the server).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..analyzers import REGISTRY
from ..assets import AssetStore, prepare_assets
from ..assets.prepare import Config, llm_overrides
from ..coordinator import grade
from ..llm import LLMClient
from ..report import render_text
from ..rules import RulesError, load_rules

STATIC = Path(__file__).parent / "static"
MAX_BODY = 2 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}\.c$")
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".autograder-cache", "reports", "sample_outputs"}


class BadRequest(ValueError):
    def __init__(self, message: str, problems: Optional[List[str]] = None):
        super().__init__(message)
        self.problems = problems or []


def find_rules_files(root: Path, max_depth: int = 3) -> List[str]:
    out = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for p in sorted(d.iterdir()):
            if p.is_dir() and p.name not in _SKIP_DIRS and not p.name.startswith("."):
                walk(p, depth + 1)
            elif p.is_file() and p.name == "rules.json":
                out.append(p.relative_to(root).as_posix())

    walk(root, 0)
    return out


def check_uploaded_rules(raw: Any) -> List[str]:
    """Uploaded rules may only refer to files next to themselves."""
    if not isinstance(raw, dict):
        return ["rules must be a JSON object"]
    problems = []
    if raw.get("plugins"):
        problems.append("'plugins' are not allowed in uploaded rules (they run Python); "
                        "use a rules file from the server's folder instead")

    def scan(v, where):
        if isinstance(v, str) and (v.startswith(("/", "~")) or ".." in Path(v).parts):
            problems.append(f"{where}: absolute or '..' paths are not allowed in uploaded rules")

    for key in ("reference", "statement"):
        scan(raw.get(key), f"rules.{key}")
    for i, m in enumerate(raw.get("metrics", []) if isinstance(raw.get("metrics"), list) else []):
        if isinstance(m, dict):
            for k, v in (m.get("params") or {}).items():
                if k in ("testset_dir", "cases_file", "generator", "corpus_dir", "known_dir"):
                    scan(v, f"rules.metrics[{i}].params.{k}")
    return problems


def host_path(p: Path) -> str:
    """Where a file inside the working folder lives on the user's machine.
    In Docker the folder is mounted at /work; grade.sh passes the host side as
    AUTOGRADER_HOST_WORKDIR (e.g. C:/Users/me/course or /home/me/course)."""
    p = Path(p).resolve()
    host = os.environ.get("AUTOGRADER_HOST_WORKDIR", "").strip()
    if not host:
        return str(p)
    try:
        rel = p.relative_to(Path.cwd().resolve())
    except ValueError:
        return str(p)
    sep = "\\" if "\\" in host else "/"
    return host.rstrip("/\\") + sep + sep.join(rel.parts)


class App:
    def __init__(self, root: Path, cache_root: Path, offline_default: bool = False,
                 llm_defaults: Optional[Dict[str, str]] = None):
        self.root = root.resolve()
        self.cache_root = cache_root
        self.offline_default = offline_default
        self.llm_defaults = llm_overrides(llm_defaults)
        self.store = AssetStore(cache_root / "autograder.sqlite3")
        self._llms: Dict[bool, LLMClient] = {}
        self._lock = threading.Lock()

    def llm(self, offline: bool) -> LLMClient:
        with self._lock:
            if offline not in self._llms:
                self._llms[offline] = LLMClient(self.cache_root / "llm", offline=offline)
            return self._llms[offline]

    # -- grading ------------------------------------------------------------
    def resolve_rules(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root not in p.parents or p.suffix != ".json" or not p.is_file():
            raise BadRequest(f"rules file not found under the server folder: {rel}")
        return p

    def grade(self, body: Dict[str, Any]) -> Dict[str, Any]:
        sub = body.get("submission") or {}
        name, source = sub.get("name", "submission.c"), sub.get("source")
        if not isinstance(source, str) or not source.strip():
            raise BadRequest("submission source is empty")
        if not isinstance(name, str) or not _NAME.match(name):
            raise BadRequest("submission name must look like name.c (letters, digits, _ . -)")
        rules_in = body.get("rules") or {}
        generate = bool(body.get("generate_assets"))
        offline = bool(body.get("offline", self.offline_default))
        overrides = self.overrides(body)

        tmp = Path(tempfile.mkdtemp(prefix="ag-web-"))
        try:
            if isinstance(rules_in.get("path"), str):
                rules_path = self.resolve_rules(rules_in["path"])
                sub_dir = tmp
            elif isinstance(rules_in.get("json"), str):
                try:
                    raw = json.loads(rules_in["json"])
                except ValueError as e:
                    raise BadRequest(f"rules are not valid JSON: {e}")
                problems = check_uploaded_rules(raw)
                if problems:
                    raise BadRequest("uploaded rules rejected", problems)
                rules_path = tmp / "rules.json"
                rules_path.write_text(rules_in["json"])
                sub_dir = tmp
            else:
                raise BadRequest("choose a rules file or upload one")
            submission = self._in_corpus(rules_path, name, source) or sub_dir / name
            if submission.parent == sub_dir:
                submission.write_text(source)

            llm = self.llm(offline)
            prep = None
            if generate:
                prep = prepare_assets(rules_path, llm, self.store, self.cache_root, submission, overrides)
            try:
                rules = load_rules(rules_path, REGISTRY, prep.overlay if prep else None)
            except RulesError as e:
                hint = [] if generate else ["tick 'generate missing assets' to let the grader prepare them"]
                raise BadRequest("the rules file is not usable", e.problems + hint)
            report = grade(submission, rules, llm)
            if prep is not None and prep.overlay is not None:
                report["meta"]["assets"] = {"cache_hit": prep.cache_hit, "bundle_dir": host_path(prep.bundle_dir)}
            grade_id = self.store.record_grade(report)
            return {"id": grade_id, "report": report, "text": render_text(report)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _in_corpus(self, rules_path: Path, name: str, source: str) -> Optional[Path]:
        """The uploaded file IS a file of the class corpus (same name, same
        bytes): grade it in place, as the CLI would, so similarity does not
        compare it with itself. A differently named copy is still flagged."""
        try:
            raw = json.loads(rules_path.read_text())
        except (OSError, ValueError):
            return None
        for m in raw.get("metrics", []) if isinstance(raw, dict) else []:
            d = (m.get("params") or {}).get("corpus_dir") if isinstance(m, dict) else None
            if isinstance(d, str) and d:
                p = (rules_path.parent / d / name).resolve()
                if p.is_file() and p.read_text(errors="replace") == source:
                    return p
        return None

    def overrides(self, body: Dict[str, Any]) -> Dict[str, str]:
        out = dict(self.llm_defaults)
        for key in ("provider", "model"):
            v = body.get(f"llm_{key}")
            if isinstance(v, str) and v.strip():
                if not re.fullmatch(r"[A-Za-z0-9_.:/@+-]{1,100}", v.strip()):
                    raise BadRequest(f"llm_{key} contains unexpected characters")
                out[key] = v.strip()
        return out

    def llm_status(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        body = {f"llm_{k}": v[0] for k, v in query.items() if k in ("provider", "model") and v}
        cfg, problems = Config.from_rules({}, self.overrides(body))
        if problems:
            return {"ok": False, "provider": cfg.provider, "model": cfg.model, "error": "; ".join(problems)}
        offline = query.get("offline", ["0"])[0] in ("1", "true")
        return self.llm(offline).check(cfg.provider, cfg.model)

    # -- reading -------------------------------------------------------------
    def grade_detail(self, grade_id: int) -> Dict[str, Any]:
        row = self.store.grade(grade_id)
        if row is None:
            raise LookupError(f"no grade {grade_id}")
        return {"id": row["id"], "created_at": row["created_at"], "report": row["report"],
                "text": render_text(row["report"])}

    def grade_text(self, grade_id: int) -> Tuple[str, str]:
        """(filename, text report) for a download."""
        row = self.store.grade(grade_id)
        if row is None:
            raise LookupError(f"no grade {grade_id}")
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(row["report"]["submission"]).stem) or "report"
        return f"{stem}_grade{grade_id}_report.txt", render_text(row["report"])

    def bundle_detail(self, key: str) -> Dict[str, Any]:
        b = self.store.get_bundle(key)
        if b is None:
            raise LookupError(f"no bundle {key}")
        manifest = b["manifest"]
        storage = self.cache_root / manifest["storage"] if manifest.get("storage") else None
        assets = []
        for a in self.store.assets(key):
            content = a.content or ""
            on_disk = storage is not None and a.path is not None and (storage / a.path).is_file()
            assets.append({
                "kind": a.kind, "name": a.name, "status": a.status, "origin": a.origin, "detail": a.detail,
                "group": "instructor" if a.origin.startswith("instructor") else "generated",
                "path": a.path, "file": host_path(storage / a.path) if on_disk else None,
                "content": content[:20000], "truncated": len(content) > 20000, "size": len(content)})
        files = {}
        if storage is not None:
            for name in ("manifest.json", "validation/report.json"):
                if (storage / name).is_file():
                    files[name] = host_path(storage / name)
        return {"bundle_key": key, "generation_id": manifest.get("generation_id", key[:16]),
                "problem": b["problem"], "model": b["model_name"], "provider": b["model_provider"],
                "model_digest": b["model_digest"], "created_at": b["created_at"],
                "generator_version": b["generator_version"], "dependencies": b["dependencies"],
                "storage_dir": host_path(storage) if storage is not None and storage.is_dir() else None,
                "summary_files": files,
                "manifest": {k: v for k, v in manifest.items() if k != "params"},
                "assets": assets, "events": self.store.events(key)}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AutoGrader"

        def log_message(self, fmt, *args):  # quieter than the default
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _download(self, filename: str, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> Tuple[str, List[str]]:
            path = urlparse(self.path).path
            return path, [p for p in path.split("/") if p]

        def do_GET(self):
            path, parts = self._route()
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if parts[:1] != ["api"]:
                    return self._json(404, {"error": "not found"})
                if parts == ["api", "problems"]:
                    cfg, _ = Config.from_rules({}, app.llm_defaults)
                    return self._json(200, {"root": str(app.root), "rules": find_rules_files(app.root),
                                            "offline_default": app.offline_default,
                                            "llm": {"provider": cfg.provider, "model": cfg.model}})
                if parts == ["api", "grades"]:
                    return self._json(200, {"grades": list(reversed(app.store.grades()))})
                if len(parts) == 3 and parts[1] == "grades" and parts[2].isdigit():
                    return self._json(200, app.grade_detail(int(parts[2])))
                if len(parts) == 4 and parts[1] == "grades" and parts[2].isdigit() and parts[3] == "report.txt":
                    return self._download(*app.grade_text(int(parts[2])))
                if parts == ["api", "llm"]:
                    return self._json(200, app.llm_status(parse_qs(urlparse(self.path).query)))
                if parts == ["api", "bundles"]:
                    return self._json(200, {"bundles": app.store.bundles()})
                if len(parts) == 3 and parts[1] == "bundles" and re.fullmatch(r"[0-9a-f]{64}", parts[2]):
                    return self._json(200, app.bundle_detail(parts[2]))
                return self._json(404, {"error": "not found"})
            except LookupError as e:
                return self._json(404, {"error": str(e)})
            except BadRequest as e:
                return self._json(400, {"error": str(e)})

        def do_POST(self):
            path, _ = self._route()
            if path != "/api/grade":
                return self._json(404, {"error": "not found"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(415, {"error": "send application/json"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": f"request larger than {MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                return self._json(200, app.grade(body))
            except BadRequest as e:
                return self._json(400, {"error": str(e), "problems": e.problems})
            except ValueError as e:
                return self._json(400, {"error": f"bad request: {e}"})
            except Exception as e:  # noqa: BLE001 - report, don't kill the server
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def make_server(app: App, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server


def serve(root: Path, cache_root: Path, host: str, port: int, offline: bool,
          llm_defaults: Optional[Dict[str, str]] = None) -> None:
    app = App(root, cache_root, offline, llm_defaults)
    server = make_server(app, host, port)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = os.environ.get("AUTOGRADER_UI_URL") or f"http://{shown}:{server.server_address[1]}"  # Docker: host port
    print(f"AutoGrader UI on {url}  (folder {app.root}, Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
