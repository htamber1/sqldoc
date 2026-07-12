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
CREATE TABLE IF NOT EXISTS table_sizes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    db_name  TEXT NOT NULL,
    at       TEXT NOT NULL,
    obj      TEXT NOT NULL,
    size_mb  REAL,
    rows     INTEGER
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    command  TEXT NOT NULL,
    dialect  TEXT,
    database TEXT,
    user     TEXT,
    options  TEXT,
    result   TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit(at);
CREATE INDEX IF NOT EXISTS ix_events_db_at ON events(db_name, at);
CREATE INDEX IF NOT EXISTS ix_metrics_db_at ON metrics(db_name, at);
CREATE INDEX IF NOT EXISTS ix_runs_db ON runs(db_name, id);
CREATE INDEX IF NOT EXISTS ix_tablesizes_db_at ON table_sizes(db_name, at);
"""

# Capacity columns added to `metrics` after the fact (see _migrate).
_METRIC_MIGRATIONS = [
    ("database_size_mb", "REAL"),
    ("disk_free_mb", "REAL"),
    ("disk_total_mb", "REAL"),
    ("max_size_mb", "REAL"),
    ("fragmentation_avg", "REAL"),
]


class AgentStore:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    def _migrate(self, c):
        """Add capacity columns to `metrics` on existing databases."""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(metrics)").fetchall()}
        for name, typ in _METRIC_MIGRATIONS:
            if name not in cols:
                c.execute(f"ALTER TABLE metrics ADD COLUMN {name} {typ}")

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

    def events_since(self, since_iso: str, db_name: str = None) -> list:
        """Events at/after an ISO-8601 timestamp (oldest first). ISO strings from
        _now() sort lexicographically, so a string comparison is a time filter."""
        with self._conn() as c:
            if db_name:
                rows = c.execute(
                    "SELECT * FROM events WHERE at >= ? AND db_name=? ORDER BY id ASC",
                    (since_iso, db_name)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM events WHERE at >= ? ORDER BY id ASC",
                    (since_iso,)).fetchall()
        return [dict(r) for r in rows]

    def metrics_since(self, since_iso: str, db_name: str) -> list:
        """Metric rows at/after an ISO timestamp for one database (oldest first)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM metrics WHERE db_name=? AND at >= ? ORDER BY id ASC",
                (db_name, since_iso)).fetchall()
        return [dict(r) for r in rows]

    # --- key/value meta (agent bookkeeping) --------------------------------

    def get_meta(self, key: str):
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str):
        with self._conn() as c:
            c.execute("INSERT INTO kv(key, value) VALUES (?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    # --- audit trail -------------------------------------------------------

    def add_audit(self, at, command, dialect=None, database=None, user=None,
                  options=None, result=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO audit(at, command, dialect, database, user, options, result) "
                "VALUES (?,?,?,?,?,?,?)",
                (at, command, dialect, database, user,
                 json.dumps(options) if options is not None else None, result))

    def query_audit(self, command=None, database=None, since=None, limit=1000) -> list:
        clauses, params = [], []
        if command:
            clauses.append("command=?"); params.append(command)
        if database:
            clauses.append("database=?"); params.append(database)
        if since:
            clauses.append("at >= ?"); params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM audit{where} ORDER BY id DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

    # --- metrics (trends) --------------------------------------------------

    def add_metric(self, db_name: str, tables=0, columns=0, pii_high=0, pii_medium=0,
                   pii_low=0, pii_score=0.0, health_issues=0, health_degraded=0,
                   database_size_mb=None, disk_free_mb=None, disk_total_mb=None,
                   max_size_mb=None, fragmentation_avg=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO metrics(db_name, at, tables, columns, pii_high, pii_medium, "
                "pii_low, pii_score, health_issues, health_degraded, database_size_mb, "
                "disk_free_mb, disk_total_mb, max_size_mb, fragmentation_avg) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (db_name, _now(), tables, columns, pii_high, pii_medium, pii_low,
                 pii_score, health_issues, health_degraded, database_size_mb,
                 disk_free_mb, disk_total_mb, max_size_mb, fragmentation_avg))

    def add_table_sizes(self, db_name: str, sizes: list):
        """`sizes` is a list of (obj, size_mb, rows) tuples for one poll."""
        at = _now()
        with self._conn() as c:
            c.executemany(
                "INSERT INTO table_sizes(db_name, at, obj, size_mb, rows) VALUES (?,?,?,?,?)",
                [(db_name, at, obj, size_mb, rows) for obj, size_mb, rows in sizes])

    def table_size_history(self, db_name: str, limit: int = 5000) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT at, obj, size_mb, rows FROM table_sizes WHERE db_name=? "
                "ORDER BY id ASC LIMIT ?", (db_name, limit)).fetchall()
        return [dict(r) for r in rows]

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
                "SELECT db_name FROM runs "
                "UNION SELECT db_name FROM snapshots "
                "UNION SELECT db_name FROM metrics ORDER BY db_name").fetchall()
        return [r["db_name"] for r in rows]
