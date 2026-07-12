"""HA / replication monitoring across dialects, health/lag, render, CLI, agent."""
import json

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.ha import collect_ha, summarize, behind_replicas
from sqldoc.ha_renderer import build_ha_json, render_ha_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


# --- SQL Server -------------------------------------------------------------

def test_sqlserver_ha(fake_mssql_ha_rows):
    report = collect_ha(FakeAdapter(FakeConnection(fake_mssql_ha_rows), dialect="sqlserver"))
    assert report.ha_enabled and report.mechanism.startswith("Always On")
    reps = {r.server: r for r in report.replicas}
    assert reps["SQLNODE1"].role == "PRIMARY" and reps["SQLNODE1"].is_healthy
    assert reps["SQLNODE2"].is_healthy                 # HEALTHY secondary with small queue
    assert not reps["SQLNODE3"].is_healthy             # NOT_HEALTHY
    assert reps["SQLNODE2"].lag_bytes == 150 * 1024    # send + redo queue
    s = summarize(report)
    assert s["replicas"] == 3 and s["unhealthy"] == 1 and s["secondaries"] == 2


def test_sqlserver_ha_none():
    report = collect_ha(FakeAdapter(FakeConnection({}), dialect="sqlserver"))
    assert not report.ha_enabled and report.notes


# --- PostgreSQL -------------------------------------------------------------

def test_postgres_ha(fake_pg_ha_rows):
    report = collect_ha(FakeAdapter(FakeConnection(fake_pg_ha_rows), dialect="postgres"))
    assert report.ha_enabled
    reps = {r.server: r for r in report.replicas}
    assert reps["standby1"].is_healthy and reps["standby1"].lag_seconds == 0.5
    # streaming but 120s behind -> healthy connection, but flagged as behind
    assert reps["standby2"].is_healthy and reps["standby2"].lag_seconds == 120.0
    behind = behind_replicas(report, threshold_seconds=30)
    assert {r.server for r in behind} == {"standby2"}


# --- MySQL ------------------------------------------------------------------

def test_mysql_ha(fake_mysql_ha_rows):
    report = collect_ha(FakeAdapter(FakeConnection(fake_mysql_ha_rows), dialect="mysql"))
    assert report.ha_enabled
    r = report.replicas[0]
    assert r.io_running == "Yes" and r.sql_running == "Yes"
    assert r.lag_seconds == 45.0 and r.is_healthy
    behind = behind_replicas(report, threshold_seconds=30)
    assert len(behind) == 1                            # 45s > 30s


def test_unsupported_dialect():
    report = collect_ha(FakeAdapter(FakeConnection({}), dialect="sqlite"))
    assert not report.supported


# --- render + json + CLI ----------------------------------------------------

def test_build_and_render(fake_mssql_ha_rows, tmp_path):
    report = collect_ha(FakeAdapter(FakeConnection(fake_mssql_ha_rows), dialect="sqlserver"))
    data = build_ha_json("SRV", report)
    assert data["report_type"] == "ha" and data["ha_enabled"]
    assert any(not r["is_healthy"] for r in data["replicas"])

    out = tmp_path / "ha.html"
    render_ha_html("SRV", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "High Availability" in h and "SQLNODE1" in h and "unhealthy" in h


def test_ha_cli(monkeypatch, fake_mssql_ha_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_mssql_ha_rows)))
    out = tmp_path / "ha.html"
    jout = tmp_path / "ha.json"
    res = CliRunner().invoke(cli.cli, [
        "ha", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Replicas: 3" in res.output and "Unhealthy: 1" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["summary"]["unhealthy"] == 1


def test_ha_cli_none(monkeypatch, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection({})))
    res = CliRunner().invoke(cli.cli, [
        "ha", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(tmp_path / "ha.html"),
    ])
    assert res.exit_code == 0, res.output
    assert "No replication configured" in res.output
