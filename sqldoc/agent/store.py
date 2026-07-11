"""SQLite-backed state for the sqldoc agent.

One database (default ``~/.sqldoc/agent.db``) holds everything the daemon and
dashboard need: the latest schema snapshot + AI-description cache per monitored
database, the run history, a change/alert event timeline, a metrics time-series
(for health + PII trends), and the latest rendered documentation HTML.

Every method opens its own short-lived connection, so the store is safe to share
across the daemon's poller threads and the dashboard's request threads without
locking. WAL mode keeps concurrent readers and the writer from blocking.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    db_name       TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS caches (
    db_name    TEXT PRIMARY KEY,
    cache_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS docs (
    db_name    TEXT PRIMARY KEY,
    html       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pii_snapshots (
    db_name   TEXT PRIMARY KEY,
    snap_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    db_name     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    db_name   TEXT NOT NULL,
    at        TEXT NOT NULL,
    type      TEXT NOT NULL,
    summary   TEXT NOT NULL,
    detail    TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    db_name        TEXT NOT NULL,
    at             TEXT NOT NULL,
    tables         INTEGER,
    columns        INTEGER,
    pii_high       INTEGER,
    pii_medium     INTEGER,
    pii_low        INTEGER,
    pii_score      REAL,
    health_issues  INTEGER,
    health_degraded INTEGER
);
CREATE INDEX IF NOT EXISTS ix_events_db_at ON events(db_name, at);
CREATE INDEX IF NOT EXISTS ix_metrics_db_at ON metrics(db_name, at);
CREATE INDEX IF NOT EXISTS ix_runs_db ON runs(db_name, id);
"""


class AgentStore:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- snapshots + cache -------------------------------------------------

    def get_snapshot(self, db_name: str):
        with self._conn() as c:
            row = c.execute("SELECT snapshot_json FROM snapshots WHERE db_name=?",
                            (db_name,)).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def save_snapshot(self, db_name: str, snapshot: dict):
        with self._conn() as c:
            c.execute(
                "INSERT INTO snapshots(db_name, snapshot_json, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(db_name) DO UPDATE SET snapshot_json=excluded.snapshot_json, "
                "updated_at=excluded.updated_at",
                (db_name, json.dumps(snapshot), _now()))

    def get_cache(self, db_name: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT cache_json FROM caches WHERE db_name=?",
                            (db_name,)).fetchone()
        return json.loads(row["cache_json"]) if row else {}

    def save_cache(self, db_name: str, cache: dict):
        with self._conn() as c:
            c.execute(
                "INSERT INTO caches(db_name, cache_json, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(db_name) DO UPDATE SET cache_json=excluded.cache_json, "
                "updated_at=excluded.updated_at",
                (db_name, json.dumps(cache), _now()))

    def get_pii_snapshot(self, db_name: str):
        with self._conn() as c:
            row = c.execute("SELECT snap_json FROM pii_snapshots WHERE db_name=?",
                            (db_name,)).fetchone()
        return json.loads(row["snap_json"]) if row else None

    def save_pii_snapshot(self, db_name: str, snap: dict):
        with self._conn() as c:
            c.execute(
                "INSERT INTO pii_snapshots(db_name, snap_json, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(db_name) DO UPDATE SET snap_json=excluded.snap_json, "
                "updated_at=excluded.updated_at",
                (db_name, json.dumps(snap), _now()))

    # --- docs --------------------------------------------------------------

    def save_doc(self, db_name: str, html: str):
        with self._conn() as c:
            c.execute(
                "INSERT INTO docs(db_name, html, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(db_name) DO UPDATE SET html=excluded.html, "
                "updated_at=excluded.updated_at",
                (db_name, html, _now()))

    def get_doc(self, db_name: str):
        with self._conn() as c:
            row = c.execute("SELECT html, updated_at FROM docs WHERE db_name=?",
                            (db_name,)).fetchone()
        return (row["html"], row["updated_at"]) if row else (None, None)

    # --- runs --------------------------------------------------------------

    def start_run(self, db_name: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO runs(db_name, started_at) VALUES (?,?)",
                            (db_name, _now()))
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str = "ok", error: str = None):
        with self._conn() as c:
            c.execute("UPDATE runs SET finished_at=?, status=?, error=? WHERE id=?",
                      (_now(), status, error, run_id))

    def last_run(self, db_name: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM runs WHERE db_name=? ORDER BY id DESC LIMIT 1",
                (db_name,)).fetchone()
        return dict(row) if row else None

    # --- events (timeline) -------------------------------------------------

    def add_event(self, db_name: str, type: str, summary: str, detail=None):
        with self._conn() as c:
            c.execute("INSERT INTO events(db_name, at, type, summary, detail) VALUES (?,?,?,?,?)",
                      (db_name, _now(), type, summary,
                       json.dumps(detail) if detail is not None else None))

    def recent_events(self, db_name: str = None, limit: int = 50) -> list:
        with self._conn() as c:
            if db_name:
                rows = c.execute(
                    "SELECT * FROM events WHERE db_name=? ORDER BY id DESC LIMIT ?",
                    (db_name, limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- metrics (trends) --------------------------------------------------

    def add_metric(self, db_name: str, tables=0, columns=0, pii_high=0, pii_medium=0,
                   pii_low=0, pii_score=0.0, health_issues=0, health_degraded=0):
        with self._conn() as c:
            c.execute(
                "INSERT INTO metrics(db_name, at, tables, columns, pii_high, pii_medium, "
                "pii_low, pii_score, health_issues, health_degraded) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (db_name, _now(), tables, columns, pii_high, pii_medium, pii_low,
                 pii_score, health_issues, health_degraded))

    def metrics_history(self, db_name: str, limit: int = 200) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM metrics WHERE db_name=? ORDER BY id DESC LIMIT ?",
                (db_name, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def latest_metric(self, db_name: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM metrics WHERE db_name=? ORDER BY id DESC LIMIT 1",
                (db_name,)).fetchone()
        return dict(row) if row else None

    # --- overview ----------------------------------------------------------

    def list_databases(self) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT db_name FROM runs "
                "UNION SELECT db_name FROM snapshots ORDER BY db_name").fetchall()
        return [r["db_name"] for r in rows]
