"""Large-table safety + connection-resilience for `sqldoc quality`.

Covers the four dev-server-validation fixes:
  01 - reconnect on a dropped connection (no cascading link-failure skips).
  02 - `backup --database` defaults to master (instance-level command).
  03 - row-count threshold skips full-row duplicate detection on huge tables.
  04 - row-count threshold for heavy per-column stats (approx / skip distinct,
       skip MIN/MAX + top-values), with a runtime APPROX_COUNT_DISTINCT probe.

All fakes are self-contained here (no live DB); the recording cursor lets us
assert exactly which SQL each branch emits.
"""
import pytest

from sqldoc import quality, cli
from sqldoc.extractor import Table, Column
from sqldoc.quality import (
    analyze_column_quality, detect_duplicates, collect_quality,
    _is_connection_lost, _detect_approx_distinct, _DUP_SKIPPED_TOO_LARGE,
    _SQLSERVER, _MYSQL,
)


# --- Fakes ------------------------------------------------------------------

class FakeOdbcError(Exception):
    """Mimics a pyodbc error: args = (sqlstate, message)."""


class _Row:
    def __init__(self, **kw):
        self._d = kw

    def __getitem__(self, k):
        return self._d[k] if isinstance(k, str) else list(self._d.values())[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


class RecCursor:
    """Records every SQL string and returns plausible rows for the quality
    queries. Can be told to raise a connection-lost error on its first stats
    query (to simulate a mid-run TCP drop)."""

    def __init__(self, approx_ok=True, drop_on_stats=False):
        self.executed = []
        self._approx_ok = approx_ok
        self._drop_on_stats = drop_on_stats
        self._last = None

    def execute(self, sql, *params):
        self.executed.append(sql)
        if " AS ok" in sql:
            self._last = "probe"
        elif "dup_rows" in sql:
            self._last = "dup"
        elif "non_null" in sql:
            if self._drop_on_stats:
                raise FakeOdbcError(
                    "08S01",
                    "[08S01][Microsoft][ODBC Driver 17 for SQL Server]"
                    "TCP Provider: An established connection was aborted ... "
                    "Communication link failure (0)")
            self._last = "stats"
        elif " AS freq" in sql:
            self._last = "top"
        else:
            self._last = "unknown"
        return self

    def fetchall(self):
        if self._last == "probe":
            return [_Row(ok=1 if self._approx_ok else 0)]
        if self._last == "stats":
            return [_Row(total=100, non_null=80, distinct_count=7,
                         blank_count=0, min_val="a", max_val="z")]
        if self._last == "top":
            return [_Row(val="x", freq=10)]
        if self._last == "dup":
            return [_Row(dup_rows=8, dup_groups=3)]
        return []

    # convenience for assertions
    def sql_matching(self, needle):
        return [s for s in self.executed if needle in s]


class DummyConn:
    def close(self):
        pass


class SimpleAdapter:
    """Hands back the same recording cursor every time — so a test can inspect
    all SQL that a whole `collect_quality` run emitted."""

    def __init__(self, cursor, dialect="sqlserver"):
        self.dialect = dialect
        self._cursor = cursor
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        return DummyConn()

    def cursor(self, conn):
        return self._cursor


class QueueAdapter:
    """Yields a queue of cursors across (re)connects, so a mid-run reconnect
    lands on a fresh, healthy cursor."""

    def __init__(self, cursors, dialect="sqlserver", fail_reconnect=False):
        self.dialect = dialect
        self._cursors = list(cursors)
        self.connect_calls = 0
        self._fail_reconnect = fail_reconnect

    def connect(self):
        self.connect_calls += 1
        if self._fail_reconnect and self.connect_calls > 1:
            raise FakeOdbcError("08S01", "Communication link failure")
        return DummyConn()

    def cursor(self, conn):
        return self._cursors.pop(0) if self._cursors else RecCursor()


def _big_table(rows=100_000_000):
    return Table(
        schema="dbo", name="Events", row_count=rows,
        columns=[
            Column("Id", "int", 4, False, True, False, None, None),
            Column("Amount", "int", 4, True, False, False, None, None),
        ],
    )


def _small_table(rows=1000):
    return Table(
        schema="dbo", name="Small", row_count=rows,
        columns=[Column("Id", "int", 4, False, True, False, None, None)],
    )


# --- 01: connection classification + reconnect ------------------------------

def test_is_connection_lost_by_sqlstate_and_message():
    assert _is_connection_lost(FakeOdbcError("08S01", "link failure"))
    assert _is_connection_lost(FakeOdbcError("HYT00", "timeout"))
    # message-only (some drivers don't set a clean SQLSTATE)
    assert _is_connection_lost(Exception("server has gone away"))
    assert _is_connection_lost(Exception("WSAECONNRESET 10054"))
    # a genuine per-column data error is NOT a connection loss
    assert not _is_connection_lost(FakeOdbcError("42000", "Invalid column"))
    assert not _is_connection_lost(ValueError("cannot GROUP BY a text column"))


def test_collect_quality_reconnects_on_transient_drop():
    """One mid-run TCP drop -> reconnect + retry that column; every column still
    profiled, no cascade, no false errors."""
    dropping = RecCursor(drop_on_stats=True)   # dies on its first stats query
    healthy = RecCursor(drop_on_stats=False)
    adapter = QueueAdapter([dropping, healthy])

    report = collect_quality(adapter, [_small_table(), _small_table()])

    assert report.errors == []                 # retry succeeded -> no error rows
    assert len(report.columns) == 2            # both tables' single column done
    assert adapter.connect_calls == 2          # initial + exactly one reconnect


def test_collect_quality_aborts_cleanly_on_unrecoverable_outage(monkeypatch):
    """If reconnection is impossible, record ONE honest summary error and stop —
    never thousands of cascading link-failure skips."""
    monkeypatch.setattr(quality.time, "sleep", lambda *a, **k: None)  # no backoff wait
    dropping = RecCursor(drop_on_stats=True)
    adapter = QueueAdapter([dropping], fail_reconnect=True)

    report = collect_quality(adapter, [_small_table(), _small_table()])

    assert len(report.columns) == 0
    summaries = [c for c, _ in report.errors if c == "connection"]
    assert len(summaries) == 1                 # one honest summary, no cascade
    assert "could not be re-established" in dict(report.errors)["connection"]
    assert len(report.errors) <= 2             # (triggering column + summary), not thousands


# --- 02: backup --database defaults to master -------------------------------

def _db_default(command_name):
    cmd = cli.cli.commands[command_name]
    opt = next(p for p in cmd.params if getattr(p, "name", None) == "database")
    return opt.default


def test_backup_database_defaults_to_master():
    assert _db_default("backup") == "master"


def test_instance_level_commands_share_master_default():
    # backup must be consistent with its instance-level siblings
    for name in ("server", "logs", "secure", "waits", "plans", "backup"):
        assert _db_default(name) == "master", f"{name} --database default"


# --- 03: duplicate-detection row-count threshold ----------------------------

def test_detect_duplicates_skipped_above_threshold_without_querying():
    cur = RecCursor()
    cols = [Column("Id", "int", 4, False, True, False, None, None)]
    out = detect_duplicates(cur, "dbo", "Events", cols,
                            row_count=100_000_000, max_rows=5_000_000)
    assert out is _DUP_SKIPPED_TOO_LARGE
    assert cur.executed == []                  # returned BEFORE any query


def test_detect_duplicates_runs_below_threshold():
    cur = RecCursor()
    cols = [Column("Id", "int", 4, False, True, False, None, None)]
    dg = detect_duplicates(cur, "dbo", "Small", cols,
                           row_count=1000, max_rows=5_000_000)
    assert dg is not None and dg.duplicate_groups == 3
    assert cur.sql_matching("dup_rows")        # the heavy GROUP BY did run


def test_detect_duplicates_guard_disabled_with_zero_max_rows():
    cur = RecCursor()
    cols = [Column("Id", "int", 4, False, True, False, None, None)]
    dg = detect_duplicates(cur, "dbo", "Events", cols,
                           row_count=100_000_000, max_rows=0)
    assert dg is not None                       # 0 = no limit -> runs anyway


def test_collect_quality_notes_skipped_duplicates_on_huge_table():
    cur = RecCursor(approx_ok=False)            # keep heavy note in "skip" form
    adapter = SimpleAdapter(cur)
    report = collect_quality(adapter, [_big_table()],
                             dup_max_rows=5_000_000, heavy_max_rows=0)
    dup_notes = [msg for key, msg in report.errors if key.endswith("(duplicates)")]
    assert dup_notes and "too large for duplicate check" in dup_notes[0]
    assert len(report.columns) == 2            # columns still profiled
    assert not cur.sql_matching("dup_rows")    # the all-column dup GROUP BY never ran


# --- 04: heavy per-column stats threshold + approx probe --------------------

def test_detect_approx_distinct_probe():
    assert _detect_approx_distinct(RecCursor(approx_ok=True), _SQLSERVER) is True
    assert _detect_approx_distinct(RecCursor(approx_ok=False), _SQLSERVER) is False
    # MySQL profile has no approx configured -> never probes, always False
    assert _detect_approx_distinct(RecCursor(approx_ok=True), _MYSQL) is False


def test_detect_approx_distinct_probe_error_is_false():
    class Boom(RecCursor):
        def execute(self, sql, *a):
            raise FakeOdbcError("42000", "no such function")
    assert _detect_approx_distinct(Boom(), _SQLSERVER) is False


def test_analyze_column_heavy_uses_approx_and_skips_minmax_and_top():
    cur = RecCursor()
    analyze_column_quality(cur, "dbo", "Events", "Amount", "int",
                           top_values=5, row_count=100_000_000,
                           heavy_max_rows=5_000_000, approx_distinct_ok=True)
    stats = cur.executed[0]
    assert "APPROX_COUNT_DISTINCT" in stats
    assert "COUNT(DISTINCT" not in stats       # exact distinct avoided
    assert "MIN(" not in stats and "MAX(" not in stats
    assert cur.sql_matching(" AS freq") == []  # top-values query skipped
    assert len(cur.executed) == 1


def test_analyze_column_heavy_skips_distinct_when_no_approx():
    cur = RecCursor()
    analyze_column_quality(cur, "dbo", "Events", "Amount", "int",
                           top_values=5, row_count=100_000_000,
                           heavy_max_rows=5_000_000, approx_distinct_ok=False)
    stats = cur.executed[0]
    assert "APPROX_COUNT_DISTINCT" not in stats
    assert "COUNT(DISTINCT" not in stats       # skipped -> literal -1
    assert "MIN(" not in stats
    assert cur.sql_matching(" AS freq") == []


def test_analyze_column_below_threshold_is_exact():
    cur = RecCursor()
    analyze_column_quality(cur, "dbo", "Small", "Amount", "int",
                           top_values=5, row_count=1000,
                           heavy_max_rows=5_000_000, approx_distinct_ok=True)
    stats = cur.executed[0]
    assert "COUNT(DISTINCT" in stats           # exact distinct
    assert "MIN(" in stats and "MAX(" in stats
    assert cur.sql_matching(" AS freq")        # top-values query ran


def test_collect_quality_heavy_note_approx_vs_skip():
    # approx available -> "APPROXIMATE" note
    cur_a = RecCursor(approx_ok=True)
    rep_a = collect_quality(SimpleAdapter(cur_a), [_big_table()],
                            detect_dupes=False, heavy_max_rows=5_000_000)
    heavy_a = [m for k, m in rep_a.errors if k.endswith("(heavy stats)")]
    assert heavy_a and "APPROXIMATE" in heavy_a[0]

    # approx unavailable -> "n/a (table too large)" note
    cur_s = RecCursor(approx_ok=False)
    rep_s = collect_quality(SimpleAdapter(cur_s), [_big_table()],
                            detect_dupes=False, heavy_max_rows=5_000_000)
    heavy_s = [m for k, m in rep_s.errors if k.endswith("(heavy stats)")]
    assert heavy_s and "n/a (table too large)" in heavy_s[0]
