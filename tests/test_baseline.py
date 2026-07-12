"""Performance baseline: capture, comparison, persistence, render, CLI."""
import json

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.baseline import (capture_baseline, compare_baseline, Baseline,
                             to_dict, from_dict, summarize)
from sqldoc.baseline_renderer import build_baseline_json, render_baseline_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


# --- capture ----------------------------------------------------------------

def test_capture_sqlserver(fake_mssql_baseline_rows, fake_mssql_waits_rows):
    rows = {**fake_mssql_baseline_rows, **fake_mssql_waits_rows}
    b = capture_baseline(FakeAdapter(FakeConnection(rows), dialect="sqlserver"))
    assert b.dialect == "sqlserver"
    assert b.metrics["connections"] == 25
    assert b.metrics["slowest_query_ms"] == 120.0
    assert "total_wait_ms" in b.metrics and "wait_IO_ms" in b.metrics
    assert b.metrics["job_Nightly ETL_avg_s"] == 150
    assert len(b.queries) == 2 and b.queries[0]["id"] == "0xAAAA"


# --- comparison -------------------------------------------------------------

def test_compare_flags_regressions():
    base = Baseline("sqlserver", "t0",
                    metrics={"connections": 20, "wait_IO_ms": 1000.0, "slowest_query_ms": 100.0},
                    queries=[{"id": "q1", "avg_ms": 50.0, "text": "SELECT 1"}])
    current = Baseline("sqlserver", "t1",
                       metrics={"connections": 21, "wait_IO_ms": 2000.0, "slowest_query_ms": 105.0},
                       queries=[{"id": "q1", "avg_ms": 90.0, "text": "SELECT 1"}])
    report = compare_baseline(base, current, threshold_pct=25.0)
    metrics = {a.metric: a for a in report.anomalies}
    # wait_IO doubled (+100%) -> flagged; connections +5% and slowest +5% -> not
    assert "wait_IO_ms" in metrics and metrics["wait_IO_ms"].change_pct == 100.0
    assert "connections" not in metrics and "slowest_query_ms" not in metrics
    # query q1 50->90 (+80%) -> flagged
    assert any(a.kind == "query" and a.change_pct == 80.0 for a in report.anomalies)
    s = summarize(report)
    assert s["metric_regressions"] == 1 and s["query_regressions"] == 1
    # worst-first ordering
    assert report.anomalies[0].change_pct == 100.0


def test_compare_no_regression():
    base = Baseline("mysql", "t0", metrics={"connections": 10, "total_wait_ms": 5000.0})
    current = Baseline("mysql", "t1", metrics={"connections": 11, "total_wait_ms": 5100.0})
    report = compare_baseline(base, current, threshold_pct=25.0)
    assert report.anomalies == [] and report.metrics_compared == 2


def test_persistence_roundtrip():
    b = Baseline("postgres", "t0", metrics={"connections": 5}, queries=[{"id": "q", "avg_ms": 1.0}])
    d = to_dict(b)
    assert d["type"] == "sqldoc-baseline"
    b2 = from_dict(d)
    assert b2.dialect == "postgres" and b2.metrics == {"connections": 5}


# --- render + json ----------------------------------------------------------

def test_build_and_render(tmp_path):
    base = Baseline("sqlserver", "t0", metrics={"wait_IO_ms": 1000.0})
    current = Baseline("sqlserver", "t1", metrics={"wait_IO_ms": 3000.0})
    report = compare_baseline(base, current, threshold_pct=25.0)
    data = build_baseline_json("SRV", report)
    assert data["report_type"] == "baseline" and data["summary"]["anomalies"] == 1

    out = tmp_path / "b.html"
    render_baseline_html("SRV", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Performance Baseline" in h and "wait_IO_ms" in h and "+200.0%" in h


# --- CLI --------------------------------------------------------------------

def test_baseline_cli_capture_then_compare(monkeypatch, fake_mssql_baseline_rows, fake_mssql_waits_rows, tmp_path):
    rows = {**fake_mssql_baseline_rows, **fake_mssql_waits_rows}
    monkeypatch.setattr(SqlServerAdapter, "_default_connect", staticmethod(lambda cs: FakeConnection(rows)))
    bfile = tmp_path / "base.json"

    # capture
    res = CliRunner().invoke(cli.cli, [
        "baseline", "--capture", "--server", "h", "--username", "u", "--password", "p",
        "--baseline-file", str(bfile)])
    assert res.exit_code == 0, res.output
    assert "Baseline captured" in res.output and bfile.exists()

    # compare against itself -> no regressions
    res = CliRunner().invoke(cli.cli, [
        "baseline", "--server", "h", "--username", "u", "--password", "p",
        "--baseline-file", str(bfile), "--output", str(tmp_path / "b.html")])
    assert res.exit_code == 0, res.output
    assert "Regressions: 0" in res.output


def test_baseline_cli_detects_regression_and_fails(monkeypatch, fake_mssql_baseline_rows,
                                                   fake_mssql_waits_rows, tmp_path):
    rows = {**fake_mssql_baseline_rows, **fake_mssql_waits_rows}
    monkeypatch.setattr(SqlServerAdapter, "_default_connect", staticmethod(lambda cs: FakeConnection(rows)))
    # a baseline with much lower values so current (from the fake) regresses
    bfile = tmp_path / "base.json"
    bfile.write_text(json.dumps({
        "type": "sqldoc-baseline", "dialect": "sqlserver", "captured_at": "t0",
        "metrics": {"connections": 2, "slowest_query_ms": 10.0}, "queries": []}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, [
        "baseline", "--server", "h", "--username", "u", "--password", "p",
        "--baseline-file", str(bfile), "--output", str(tmp_path / "b.html"),
        "--fail-on-regression"])
    assert res.exit_code == 1                    # regressions found -> non-zero
    assert "Regressions:" in res.output
