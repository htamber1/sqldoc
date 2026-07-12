"""Database health analysis: DMV parsing, rendering, and CLI (pyodbc mocked)."""
import json

import pytest
from click.testing import CliRunner

from sqldoc import health, cli
from sqldoc.health import detect_duplicate_tables, detect_redundant_indexes
from sqldoc.health_renderer import build_health_json, render_health_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from sqldoc.extractor import Table, Column, Index
from conftest import FakeConnection, FakeAdapter


def _col(name):
    return Column(name, "int", 4, True, False, False, None, None)


def _tbl(name, col_names, schema="dbo", indexes=None):
    return Table(schema, name, 1, columns=[_col(c) for c in col_names],
                 indexes=indexes or [])


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


# --- unused objects detector ------------------------------------------------

def test_collect_health_finds_unused_procedures(report):
    assert len(report.unused_procedures) == 1
    p = report.unused_procedures[0]
    assert p.schema == "Sales" and p.name == "uspLegacyExport"
    assert p.execution_count == 0


def test_detect_duplicate_tables():
    tables = [
        _tbl("Customer", ["Id", "Name", "Email", "Phone"]),
        _tbl("Customers", ["Id", "Name", "Email", "Phone"]),      # near-clone
        _tbl("Orders", ["OrderId", "Total", "PlacedAt"]),         # unrelated
    ]
    dups = detect_duplicate_tables(tables)
    assert len(dups) == 1
    d = dups[0]
    assert {d.table_a, d.table_b} == {"Customer", "Customers"}
    assert d.column_overlap == 1.0                                # identical columns
    assert d.name_similarity > 0.8
    assert "email" in d.shared_columns


def test_detect_duplicate_tables_ignores_dissimilar_names():
    # identical columns but wildly different names should not be flagged
    tables = [
        _tbl("Alpha", ["Id", "Name"]),
        _tbl("Zeta", ["Id", "Name"]),
    ]
    assert detect_duplicate_tables(tables) == []


def test_detect_redundant_indexes():
    ix_cust = Index("IX_Cust", "NONCLUSTERED", False, False, ["CustomerID"], [])
    ix_cust_date = Index("IX_Cust_Date", "NONCLUSTERED", False, False,
                         ["CustomerID", "OrderDate"], [])
    ix_dup = Index("IX_Cust_Copy", "NONCLUSTERED", False, False, ["CustomerID"], [])
    t = _tbl("Orders", ["CustomerID", "OrderDate"],
             indexes=[ix_cust, ix_cust_date, ix_dup])
    red = detect_redundant_indexes([t])
    names = {r.index_name: r for r in red}
    # IX_Cust is a prefix of IX_Cust_Date; IX_Cust_Copy duplicates IX_Cust
    assert "IX_Cust" in names or "IX_Cust_Copy" in names
    reasons = {r.reason for r in red}
    assert any("prefix" in x for x in reasons)


def test_redundant_indexes_leave_pk_alone():
    pk = Index("PK_Orders", "CLUSTERED", True, True, ["Id"], [])
    dup = Index("IX_Id", "NONCLUSTERED", False, False, ["Id"], [])
    t = _tbl("Orders", ["Id"], indexes=[pk, dup])
    red = detect_redundant_indexes([t])
    # the PK itself is never the one flagged as redundant
    assert all(r.index_name != "PK_Orders" for r in red)


def test_collect_health_metadata_detectors_via_tables(fake_health_rows):
    tables = [
        _tbl("Customer", ["Id", "Name", "Email"]),
        _tbl("Customer_bak", ["Id", "Name", "Email"]),
    ]
    r = health.collect_health(FakeAdapter(FakeConnection(fake_health_rows)),
                              tables=tables)
    assert len(r.duplicate_tables) == 1


def test_build_health_json(report):
    data = build_health_json("DB", report)
    assert data["report_type"] == "health" and data["database"] == "DB"
    assert data["summary"]["missing_indexes"] == 1
    assert data["missing_indexes"][0]["create_statement"].startswith("CREATE INDEX")
    assert data["fragmented_indexes"][0]["recommendation"] in ("REBUILD", "REORGANIZE")
    assert data["summary"]["unused_procedures"] == 1
    assert data["unused_procedures"][0]["name"] == "uspLegacyExport"
    assert "duplicate_tables" in data and "redundant_indexes" in data


def test_render_health_html(report, tmp_path):
    out = tmp_path / "health.html"
    render_health_html("DB", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Database Health" in h and "DB" in h
    assert "Archive" in h and "CREATE INDEX" in h
    assert "REBUILD" in h and "REORGANIZE" in h
    assert "Unused procedures" in h and "uspLegacyExport" in h
    assert "Duplicate tables" in h and "Redundant indexes" in h


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
    assert "Unused procs: 1" in res.output
    assert out.exists()
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["summary"]["dead_tables"] == 1
    assert data["summary"]["unused_procedures"] == 1
