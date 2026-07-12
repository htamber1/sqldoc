"""Instance-level server health: DMV parsing, render, CLI (pyodbc mocked)."""
import json

import pytest
from click.testing import CliRunner

from sqldoc import server, cli
from sqldoc.server_renderer import build_server_json, render_server_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


@pytest.fixture
def report(fake_server_rows):
    return server.collect_server(FakeAdapter(FakeConnection(fake_server_rows)),
                                 top=10, include_jobs=False)


def test_server_info(report):
    assert report.info.cpu_count == 8
    assert report.info.uptime_seconds == 864000
    assert report.info.uptime_text == "10d 0h 0m"


def test_cpu(report):
    assert report.cpu.sql_process_percent == 35
    assert report.cpu.other_process_percent == 10
    assert report.cpu.idle_percent == 55


def test_memory_breakdown(report):
    m = report.memory
    assert m.buffer_pool_mb == 20480.0
    assert m.plan_cache_mb == 2048.0          # CACHESTORE_SQLCP
    assert m.total_mb == 23040.0
    assert m.stolen_mb == 2560.0              # total - buffer pool


def test_volumes_and_low_disk(report):
    vols = {v.volume: v for v in report.volumes}
    assert "C:\\" in vols and "D:\\" in vols
    assert vols["D:\\"].is_low                # 4% free < 10%
    assert not vols["C:\\"].is_low            # 30% free
    # I/O latency merged by drive letter
    assert vols["D:\\"].write_latency_ms == 8.0


def test_connections_and_blocking(report):
    assert report.connections.total_sessions == 3
    assert dict(report.connections.by_login)["app"] == 2
    assert len(report.blocking_chains) == 1
    b = report.blocking_chains[0]
    assert b.blocker_session_id == 55 and b.blocked_session_id == 60
    assert b.wait_type == "LCK_M_S"
    assert "Orders" in b.blocker_query        # blocker's query resolved


def test_top_queries_sorted(report):
    assert len(report.top_queries) == 2
    assert report.top_queries[0].session_id == 55   # highest cpu first (fixture order)


def test_summarize(report):
    s = server.summarize(report)
    assert s["cpu_sql_percent"] == 35
    assert s["sessions"] == 3
    assert s["blocking_chains"] == 1
    assert s["low_disk_volumes"] == 1
    assert not report.errors


def test_collect_server_degrades_on_error(monkeypatch, fake_server_rows):
    def boom(cursor):
        raise PermissionError("VIEW SERVER STATE denied")
    monkeypatch.setattr(server, "collect_memory", boom)
    r = server.collect_server(FakeAdapter(FakeConnection(fake_server_rows)), include_jobs=False)
    assert r.memory is None
    assert r.errors and r.errors[0][0] == "Memory"
    assert r.cpu is not None                  # other checks still ran


def test_build_server_json(report):
    data = build_server_json("PRODSQL01", report)
    assert data["report_type"] == "server" and data["server"] == "PRODSQL01"
    assert data["cpu"]["sql_process_percent"] == 35
    assert any(v["is_low"] for v in data["volumes"])
    assert data["summary"]["blocking_chains"] == 1


def test_render_server_html(report, tmp_path):
    out = tmp_path / "server.html"
    render_server_html("PRODSQL01", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Server Health" in h and "PRODSQL01" in h
    assert "Blocking chains" in h and "Disk volumes" in h and "Memory" in h


# --- SQL Agent job monitoring -----------------------------------------------

@pytest.fixture
def report_with_jobs(fake_server_rows):
    return server.collect_server(FakeAdapter(FakeConnection(fake_server_rows)),
                                 top=10, include_jobs=True)


def test_agent_jobs_parsed(report_with_jobs):
    jobs = {j.name: j for j in report_with_jobs.agent_jobs}
    assert set(jobs) == {"Nightly ETL", "Backup Full", "Old Cleanup"}
    assert jobs["Nightly ETL"].last_run_status == "Failed"
    assert jobs["Backup Full"].last_run_status == "Succeeded"
    assert jobs["Old Cleanup"].enabled is False


def test_agent_job_failure_and_steps(report_with_jobs):
    etl = next(j for j in report_with_jobs.agent_jobs if j.name == "Nightly ETL")
    assert etl.failed_last_24h
    assert etl.step_failures and etl.step_failures[0].step_id == 2
    assert "duplicate key" in etl.step_failures[0].message


def test_agent_job_long_running(report_with_jobs):
    etl = next(j for j in report_with_jobs.agent_jobs if j.name == "Nightly ETL")
    assert etl.is_long_running                       # 3600s vs 1200s avg
    backup = next(j for j in report_with_jobs.agent_jobs if j.name == "Backup Full")
    assert not backup.is_long_running                # 300s == avg


def test_agent_jobs_summary(report_with_jobs):
    s = server.summarize(report_with_jobs)
    assert s["jobs"] == 3
    assert s["failed_jobs_24h"] == 1
    assert s["disabled_jobs"] == 1
    assert s["long_running_jobs"] == 1


def test_render_jobs_section(report_with_jobs, tmp_path):
    out = tmp_path / "server.html"
    render_server_html("PRODSQL01", report_with_jobs, str(out))
    h = out.read_text(encoding="utf-8")
    assert "SQL Agent jobs" in h and "Nightly ETL" in h
    assert "failed 24h" in h and "long-running" in h and "disabled" in h
    assert "Load facts" in h                         # step failure message shown


def test_server_cli_includes_jobs(monkeypatch, fake_server_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_server_rows)))
    out = tmp_path / "server.html"
    res = CliRunner().invoke(cli.cli, [
        "server", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert "Agent jobs: 3" in res.output
    assert "Failed (24h): 1" in res.output


def test_server_cli(monkeypatch, fake_server_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_server_rows)))
    out = tmp_path / "server.html"
    jout = tmp_path / "server.json"
    res = CliRunner().invoke(cli.cli, [
        "server", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "CPU (SQL): 35%" in res.output
    assert "Blocking: 1" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["report_type"] == "server"
    assert data["summary"]["low_disk_volumes"] == 1
