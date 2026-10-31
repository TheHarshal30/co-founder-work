"""Local SQLite database: generated asset bundles, their audit trail, and grades.

One file (``<cache>/autograder.sqlite3``), standard-library ``sqlite3`` only.

* ``asset_bundles``  one row per validated set of generated assets, keyed by a
  hash of everything they depend on (rules, statement, instructor assets,
  model, generator version). A changed dependency gives a new key, so stale
  assets are never reused.
* ``assets``         every asset that was generated or considered, *including
  rejected and skipped ones*, with its origin and the reason -- the audit trail.
* ``generation_events``  every LLM attempt and validation step.
* ``grades``         every report produced, with the bundle it used (if any).

Files on disk are only a materialised copy of a bundle (analyzers read files);
the database row is the record of what was validated.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS asset_bundles (
    bundle_key        TEXT PRIMARY KEY,
    problem           TEXT NOT NULL,
    rules_sha256      TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    model_provider    TEXT,
    model_name        TEXT,
    model_digest      TEXT,
    generator_version TEXT NOT NULL,
    manifest_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_key  TEXT NOT NULL REFERENCES asset_bundles(bundle_key) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    path        TEXT,
    status      TEXT NOT NULL,
    origin      TEXT NOT NULL,
    detail      TEXT,
    sha256      TEXT,
    content     TEXT
);
CREATE INDEX IF NOT EXISTS assets_by_bundle ON assets(bundle_key, status);
CREATE TABLE IF NOT EXISTS generation_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_key  TEXT NOT NULL,
    step        TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    outcome     TEXT NOT NULL,
    detail      TEXT,
    llm_cache_key TEXT
);
CREATE TABLE IF NOT EXISTS grades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    submission        TEXT NOT NULL,
    submission_sha256 TEXT NOT NULL,
    problem           TEXT NOT NULL,
    rules_hash        TEXT NOT NULL,
    assets_hash       TEXT NOT NULL,
    bundle_key        TEXT,
    grade             REAL NOT NULL,
    grade_complete    INTEGER NOT NULL,
    fingerprint       TEXT NOT NULL,
    report_json       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS grades_by_submission ON grades(submission_sha256, rules_hash);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class AssetRecord:
    kind: str     # input_spec | generator | test_input | test_output | edge_cases | inputs | oracle | known_solution
    name: str
    status: str   # validated | rejected | skipped | unavailable
    origin: str   # instructor | instructor_example | deterministic | llm | llm_oracle | instructor_reference
    detail: str = ""
    content: Optional[str] = None
    path: Optional[str] = None   # bundle-relative path it is materialised to (validated only)
    sha256: Optional[str] = None


class AssetStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(_SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self._db.close()

    # -- bundles -------------------------------------------------------------
    def get_bundle(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM asset_bundles WHERE bundle_key = ?", (key,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["manifest"] = json.loads(out.pop("manifest_json"))
        out["dependencies"] = json.loads(out.pop("dependencies_json"))
        return out

    def assets(self, key: str, status: Optional[str] = None) -> List[AssetRecord]:
        q, args = "SELECT * FROM assets WHERE bundle_key = ?", [key]
        if status:
            q += " AND status = ?"
            args.append(status)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id", args).fetchall()
        return [AssetRecord(r["kind"], r["name"], r["status"], r["origin"], r["detail"] or "",
                            r["content"], r["path"], r["sha256"]) for r in rows]

    def put_bundle(self, key: str, *, problem: str, rules_sha256: str, dependencies: Dict[str, Any],
                   provider: Optional[str], model: Optional[str], model_digest: Optional[str],
                   generator_version: str, manifest: Dict[str, Any], assets: List[AssetRecord],
                   events: List[Dict[str, Any]]) -> None:
        """Replace the bundle ``key`` atomically (all rows or none)."""
        with self._lock:
            cur = self._db.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute("DELETE FROM assets WHERE bundle_key = ?", (key,))
                cur.execute("DELETE FROM generation_events WHERE bundle_key = ?", (key,))
                cur.execute("DELETE FROM asset_bundles WHERE bundle_key = ?", (key,))
                cur.execute(
                    "INSERT INTO asset_bundles VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (key, problem, rules_sha256, json.dumps(dependencies, sort_keys=True), provider,
                     model, model_digest, generator_version, json.dumps(manifest, sort_keys=True), _now()))
                cur.executemany(
                    "INSERT INTO assets (bundle_key, kind, name, path, status, origin, detail, sha256, content) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [(key, a.kind, a.name, a.path, a.status, a.origin, a.detail, a.sha256, a.content)
                     for a in assets])
                cur.executemany(
                    "INSERT INTO generation_events (bundle_key, step, attempt, outcome, detail, llm_cache_key) "
                    "VALUES (?,?,?,?,?,?)",
                    [(key, e["step"], e["attempt"], e["outcome"], e.get("detail"), e.get("llm_cache_key"))
                     for e in events])
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def delete_bundle(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM assets WHERE bundle_key = ?", (key,))
            self._db.execute("DELETE FROM generation_events WHERE bundle_key = ?", (key,))
            self._db.execute("DELETE FROM asset_bundles WHERE bundle_key = ?", (key,))

    def events(self, key: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT step, attempt, outcome, detail, llm_cache_key FROM generation_events "
                                    "WHERE bundle_key = ? ORDER BY id", (key,)).fetchall()
        return [dict(r) for r in rows]

    # -- grades ----------------------------------------------------------------
    def record_grade(self, report: Dict[str, Any]) -> int:
        prep = report.get("asset_preparation") or {}
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO grades (submission, submission_sha256, problem, rules_hash, assets_hash, "
                "bundle_key, grade, grade_complete, fingerprint, report_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (report["submission"], report["submission_sha256"], report["problem"], report["rules_hash"],
                 report["assets_hash"], prep.get("bundle_key"), report["grade"], int(report["grade_complete"]),
                 report["fingerprint"], json.dumps(report, sort_keys=True), _now()))
            return int(cur.lastrowid)

    def grades(self, submission_sha256: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT id, submission, submission_sha256, problem, rules_hash, bundle_key, grade, " \
                  "grade_complete, fingerprint, created_at FROM grades", []
        if submission_sha256:
            q += " WHERE submission_sha256 = ?"
            args.append(submission_sha256)
        with self._lock:
            return [dict(r) for r in self._db.execute(q + " ORDER BY id", args).fetchall()]
