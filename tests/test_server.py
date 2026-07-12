"""Instance-level server health: DMV parsing, render, CLI (pyodbc mocked)."""
import json

import pytest
from click.testing import CliRunner

from sqldoc import server, cli
from sqldoc.server_renderer import build_server_json, render_server_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter, FakeRow


def _cursor(data):
    return FakeConnection(data).cursor()


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


# --- regression tests for bugs found in live SQL Server smoke testing --------

def test_cpu_all_zero_sample_not_reported_as_100_other():
    # On Linux SQL Server the ring buffer often reports 0 for both SQL and idle;
    # 100 - 0 - 0 must NOT become a misleading "100% other-process".
    cpu = server.collect_cpu(_cursor(
        {"srv_cpu": [FakeRow(sql_cpu=0, idle_cpu=0, other_cpu=100, record_id=1)]}))
    assert cpu.other_process_percent == 0
    assert cpu.sql_process_percent == 0 and cpu.idle_percent == 0


def test_volume_linux_path_label_and_latency_merge():
    # Linux: volume_mount_point is NULL, drive is "/". Must get a readable label
    # and still merge I/O latency by the drive key.
    vols = server.collect_volumes(_cursor({
        "srv_vol": [FakeRow(volume_mount_point=None, logical_volume_name=None,
                            total_gb=1000.0, available_gb=940.0, drive="/")],
        "srv_io": [FakeRow(drive="/", read_latency_ms=2.0, write_latency_ms=1.0)],
    }))
    assert len(vols) == 1
    assert vols[0].volume == "/ (root)"
    assert vols[0].read_latency_ms == 2.0 and vols[0].write_latency_ms == 1.0
    assert not vols[0].is_low                         # 94% free


def test_agent_job_next_run_zero_is_blank():
    # sysjobschedules.next_run_date is 0 until the Agent computes it (0 while the
    # Agent service is stopped) -> should render blank, not "0".
    jobs = server.collect_agent_jobs(_cursor({
        "agentjobs": [FakeRow(job_id="J", job_name="J", enabled=1, owner="sa",
                              category="c", last_run_status=1, last_run_time="t",
                              run_duration_seconds=1, avg_duration_seconds=1,
                              next_run_datetime="0")],
        "agentjobsteps": [],
    }))
    assert jobs[0].next_run_time == ""


# --- TempDB monitoring ------------------------------------------------------

def test_tempdb_collected(report):
    td = report.tempdb
    assert td is not None
    assert td.version_store_mb == 512.0
    assert td.version_generation_kb_s == 1024 and td.version_cleanup_kb_s == 1000
    assert td.data_file_count == 1 and td.recommended_files == 8
    assert td.pagelatch_contention == 3
    assert td.autogrowth_events == 5
    assert td.top_sessions[0].session_id == 70 and td.top_sessions[0].total_mb == 165.0
    # 1 data file < 8 recommended -> a note
    assert any("data file" in n for n in td.notes)


def test_tempdb_summary_and_json(report):
    s = server.summarize(report)
    assert s["tempdb_version_store_mb"] == 512.0
    assert s["tempdb_contention"] == 3 and s["tempdb_data_files"] == 1
    data = build_server_json("SRV", report)
    assert data["tempdb"]["version_store_mb"] == 512.0
    assert data["tempdb"]["top_sessions"][0]["total_mb"] == 165.0


def test_tempdb_rendered(report, tmp_path):
    out = tmp_path / "s.html"
    render_server_html("SRV", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "TempDB" in h and "Version store size" in h and "System-page contention" in h


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
