"""Database health analysis: DMV parsing, rendering, and CLI (pyodbc mocked)."""
import json

import pytest
from click.testing import CliRunner

from sqldoc import health, cli
from sqldoc.health_renderer import build_health_json, render_health_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


@pytest.fixture
def report(fake_health_rows):
    return health.collect_health(FakeAdapter(FakeConnection(fake_health_rows)), top=20)


def test_collect_health_sections(report):
    assert len(report.slow_queries) == 1
    assert report.slow_queries[0].avg_elapsed_ms == 75.0
    # dead-table filtering: Archive (writes, no reads) only; Orders active, Empty has 0 rows
    assert [d.table for d in report.dead_tables] == ["Archive"]
    assert report.dead_tables[0].reads == 0 and report.dead_tables[0].user_updates == 1200
    assert len(report.missing_indexes) == 1
    assert len(report.fragmented_indexes) == 2
    assert not report.errors


def test_missing_index_create_statement(report):
    stmt = report.missing_indexes[0].create_statement()
    assert stmt.startswith("CREATE INDEX")
    assert "[Sales].[Orders]" in stmt
    assert "CustomerID" in stmt and "OrderDate" in stmt
    assert "INCLUDE ([Total])" in stmt


def test_fragmentation_recommendation(report):
    recs = {f.index_name: f.recommendation for f in report.fragmented_indexes}
    assert recs["IX_Orders_Customer"] == "REBUILD"      # 64% >= 30
    assert recs["IX_Orders_Date"] == "REORGANIZE"       # 18% < 30


def test_collect_health_degrades_on_permission_error(monkeypatch, fake_health_rows):
    def boom(cursor, top):
        raise PermissionError("VIEW SERVER STATE denied")
    monkeypatch.setattr(health, "collect_slow_queries", boom)

    r = health.collect_health(FakeAdapter(FakeConnection(fake_health_rows)))
    assert r.slow_queries == []
    assert r.errors and r.errors[0][0] == "Slow queries"
    # other checks still ran
    assert r.dead_tables and r.fragmented_indexes


def test_schema_filter(fake_health_rows):
    r = health.collect_health(FakeAdapter(FakeConnection(fake_health_rows)), schemas=["HR"])
    assert r.dead_tables == [] and r.missing_indexes == [] and r.fragmented_indexes == []


def test_build_health_json(report):
    data = build_health_json("DB", report)
    assert data["report_type"] == "health" and data["database"] == "DB"
    assert data["summary"]["missing_indexes"] == 1
    assert data["missing_indexes"][0]["create_statement"].startswith("CREATE INDEX")
    assert data["fragmented_indexes"][0]["recommendation"] in ("REBUILD", "REORGANIZE")


def test_render_health_html(report, tmp_path):
    out = tmp_path / "health.html"
    render_health_html("DB", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Database Health" in h and "DB" in h
    assert "Archive" in h and "CREATE INDEX" in h
    assert "REBUILD" in h and "REORGANIZE" in h


def test_health_cli(monkeypatch, fake_health_rows, tmp_path):
    # The CLI builds a real SqlServerAdapter; patch its connection seam.
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_health_rows)))
    out = tmp_path / "health.html"
    jout = tmp_path / "health.json"
    res = CliRunner().invoke(cli.cli, [
        "health", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Missing indexes: 1" in res.output
    assert out.exists()
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["summary"]["dead_tables"] == 1
