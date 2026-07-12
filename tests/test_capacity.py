"""Capacity planning: collection, projection math, store, render, CLI."""
import json
from datetime import datetime, timedelta

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.capacity import (collect_capacity_snapshot, project_capacity, summarize)
from sqldoc.capacity_renderer import build_capacity_json, render_capacity_html, sparkline
from sqldoc.agent.store import AgentStore
from conftest import FakeConnection, FakeAdapter


# --- collection -------------------------------------------------------------

def test_collect_sqlserver_snapshot(fake_mssql_capacity_rows):
    snap = collect_capacity_snapshot(FakeAdapter(FakeConnection(fake_mssql_capacity_rows), dialect="sqlserver"))
    assert snap["database_size_mb"] == 20480.0 and snap["max_size_mb"] == 51200.0
    assert snap["disk_free_mb"] == 40960.0 and snap["disk_total_mb"] == 204800.0
    assert snap["fragmentation_avg"] == 23.4
    assert ("Sales.OrderLines", 12000.0, 40000000) in snap["top_tables"]


def test_collect_postgres_snapshot(fake_pg_capacity_rows):
    snap = collect_capacity_snapshot(FakeAdapter(FakeConnection(fake_pg_capacity_rows), dialect="postgres"))
    assert snap["database_size_mb"] == 5120.0
    assert snap["top_tables"][0][0] == "public.film"


def test_collect_unsupported():
    snap = collect_capacity_snapshot(FakeAdapter(FakeConnection({}), dialect="sqlite"))
    assert snap["database_size_mb"] is None and snap["top_tables"] == []


# --- projection math --------------------------------------------------------

def _mkhist():
    """Two metric points 10 days apart: db grew 100 MB, disk lost 200 MB."""
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    t1 = t0 + timedelta(days=10)
    return [
        {"at": t0.isoformat(), "database_size_mb": 1000.0, "disk_free_mb": 5000.0,
         "disk_total_mb": 10000.0, "max_size_mb": 2000.0, "fragmentation_avg": 10.0},
        {"at": t1.isoformat(), "database_size_mb": 1100.0, "disk_free_mb": 4800.0,
         "disk_total_mb": 10000.0, "max_size_mb": 2000.0, "fragmentation_avg": 20.0},
    ]


def _mktables():
    t0 = datetime(2026, 1, 1).isoformat()
    t1 = datetime(2026, 1, 11).isoformat()
    return [
        {"at": t0, "obj": "Sales.Orders", "size_mb": 400.0, "rows": 100},
        {"at": t1, "obj": "Sales.Orders", "size_mb": 500.0, "rows": 150},   # +10 MB/day
    ]


def test_project_capacity_math():
    rep = project_capacity("prod", _mkhist(), _mktables())
    assert rep.sufficient and rep.points == 2 and rep.span_days == 10.0
    # disk losing 20 MB/day, 4800 free -> 240 days
    assert rep.disk.rate_per_day == -20.0 and rep.disk.days_until_limit == 240.0
    # db growing 10 MB/day, 900 to go until 2000 max -> 90 days
    assert rep.db_size.rate_per_day == 10.0 and rep.db_size.days_until_limit == 90.0
    # fragmentation rising
    assert rep.fragmentation.current == 20.0 and rep.fragmentation.rate_per_day == 1.0
    # table growth 10 MB/day -> +300 at 30d
    g = rep.table_growth[0]
    assert g.obj == "Sales.Orders" and g.rate_mb_per_day == 10.0 and g.size_30d == 800.0


def test_project_insufficient_history():
    rep = project_capacity("prod", [{"at": "2026-01-01T00:00:00", "database_size_mb": 100.0}], [])
    assert not rep.sufficient and rep.notes


def test_sparkline():
    assert sparkline([1, 2, 3]).count(",") == 3
    assert sparkline([5]) == ""             # need >= 2 points


# --- store roundtrip --------------------------------------------------------

def test_store_capacity_roundtrip(tmp_path):
    store = AgentStore(str(tmp_path / "agent.db"))
    store.add_metric("prod", tables=5, database_size_mb=1000.0, disk_free_mb=5000.0,
                     max_size_mb=2000.0, fragmentation_avg=10.0)
    store.add_table_sizes("prod", [("Sales.Orders", 400.0, 100)])
    m = store.latest_metric("prod")
    assert m["database_size_mb"] == 1000.0 and m["fragmentation_avg"] == 10.0
    th = store.table_size_history("prod")
    assert th[0]["obj"] == "Sales.Orders" and th[0]["size_mb"] == 400.0


# --- render + json + CLI ----------------------------------------------------

def test_build_and_render(tmp_path):
    rep = project_capacity("prod", _mkhist(), _mktables())
    data = build_capacity_json([rep])
    assert data["report_type"] == "capacity"
    assert data["databases"][0]["summary"]["disk_days_until_full"] == 240.0

    out = tmp_path / "c.html"
    render_capacity_html([rep], str(out))
    h = out.read_text(encoding="utf-8")
    assert "Capacity" in h and "prod" in h and "Disk full in" in h and "<polyline" in h


def test_capacity_cli(tmp_path):
    store = AgentStore(str(tmp_path / "agent.db"))
    for row in _mkhist():
        store.add_metric("prod", database_size_mb=row["database_size_mb"],
                         disk_free_mb=row["disk_free_mb"], max_size_mb=row["max_size_mb"],
                         fragmentation_avg=row["fragmentation_avg"])
    out = tmp_path / "c.html"
    res = CliRunner().invoke(cli.cli, [
        "capacity", "--store", str(tmp_path / "agent.db"), "--output", str(out),
        "--json", str(tmp_path / "c.json"),
    ])
    assert res.exit_code == 0, res.output
    assert "prod:" in res.output and "Databases: 1" in res.output


def test_capacity_cli_no_store(tmp_path):
    res = CliRunner().invoke(cli.cli, ["capacity", "--store", str(tmp_path / "missing.db")])
    assert res.exit_code != 0
    assert "not found" in res.output
