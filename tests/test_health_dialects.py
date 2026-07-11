"""PostgreSQL + MySQL health collectors and dialect dispatch (mocked cursors)."""
import pytest

from sqldoc import health
from sqldoc.health import (collect_pg_dead_tables, collect_pg_slow_queries,
                           collect_mysql_dead_tables, collect_mysql_slow_queries,
                           collect_health)
from conftest import FakeRow, FakeAdapter


class _RoutedCursor:
    def __init__(self, data):
        self._data = data
        self._key = None

    def execute(self, sql, params=None):
        s = sql
        if "pg_stat_user_tables" in s:
            self._key = "pg_dead"
        elif "pg_stat_statements" in s:
            self._key = "pg_slow"
        elif "table_io_waits_summary_by_table" in s:
            self._key = "my_dead"
        elif "events_statements_summary_by_digest" in s:
            self._key = "my_slow"
        else:
            self._key = "unknown"
        return self

    def fetchall(self):
        return self._data.get(self._key, [])


class _Conn:
    def __init__(self, data):
        self._data = data

    def cursor(self):
        return _RoutedCursor(self._data)

    def close(self):
        pass


# --- PostgreSQL ------------------------------------------------------------

def test_pg_dead_tables_filters_read_tables():
    cur = _RoutedCursor({"pg_dead": [
        FakeRow(schema_name="public", table_name="audit", row_count=5000,
                user_seeks=0, user_scans=0, user_updates=5000, last_analyze="2026-07-11"),
        FakeRow(schema_name="public", table_name="customer", row_count=599,
                user_seeks=900, user_scans=2, user_updates=40, last_analyze="2026-07-11"),
        FakeRow(schema_name="public", table_name="empty", row_count=0,
                user_seeks=0, user_scans=0, user_updates=0, last_analyze=None),
    ]})
    dead = collect_pg_dead_tables(cur)
    assert [d.table for d in dead] == ["audit"]          # read table + empty table excluded
    assert dead[0].reads == 0 and dead[0].user_updates == 5000


def test_pg_slow_queries():
    cur = _RoutedCursor({"pg_slow": [
        FakeRow(query_text="SELECT * FROM film WHERE x=1", execution_count=120,
                total_elapsed_ms=9000.0, avg_elapsed_ms=75.0, avg_logical_reads=300,
                last_execution=""),
    ]})
    out = collect_pg_slow_queries(cur, top=10)
    assert len(out) == 1 and out[0].avg_elapsed_ms == 75.0 and out[0].execution_count == 120


# --- MySQL -----------------------------------------------------------------

def test_mysql_dead_tables_filters_read_tables():
    cur = _RoutedCursor({"my_dead": [
        FakeRow(schema_name="sakila", table_name="audit", row_count=5,
                user_scans=0, user_updates=5),
        FakeRow(schema_name="sakila", table_name="film", row_count=1000,
                user_scans=800, user_updates=10),
    ]})
    dead = collect_mysql_dead_tables(cur)
    assert [d.table for d in dead] == ["audit"]
    assert dead[0].reads == 0


def test_mysql_slow_queries_converts_picoseconds():
    cur = _RoutedCursor({"my_slow": [
        FakeRow(query_text="SELECT COUNT(*) FROM film", execution_count=50,
                total_elapsed_ms=1.0e12, avg_elapsed_ms=2.0e10,   # picoseconds/1e9 handled in SQL alias
                avg_logical_reads=1000, last_execution="2026-07-11"),
    ]})
    out = collect_mysql_slow_queries(cur, top=5)
    assert len(out) == 1 and out[0].execution_count == 50


# --- dispatch --------------------------------------------------------------

def test_collect_health_postgres_marks_unavailable_sections():
    data = {"pg_dead": [FakeRow(schema_name="public", table_name="audit", row_count=10,
                                user_seeks=0, user_scans=0, user_updates=10, last_analyze=None)],
            "pg_slow": []}
    adapter = FakeAdapter(_Conn(data), dialect="postgres")
    r = collect_health(adapter)
    assert [d.table for d in r.dead_tables] == ["audit"]
    labels = {e[0] for e in r.errors}
    assert "Missing indexes" in labels and "Index fragmentation" in labels


def test_collect_health_mysql_dispatch():
    data = {"my_dead": [FakeRow(schema_name="sakila", table_name="audit", row_count=3,
                                user_scans=0, user_updates=3)],
            "my_slow": []}
    adapter = FakeAdapter(_Conn(data), dialect="mysql")
    r = collect_health(adapter)
    assert [d.table for d in r.dead_tables] == ["audit"]
    assert any(e[0] == "Missing indexes" for e in r.errors)
