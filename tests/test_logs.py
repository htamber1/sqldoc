"""SQL Server ERRORLOG reader: parsing, critical classification, render, CLI."""
import json

import pytest
from click.testing import CliRunner

from sqldoc import logs, cli
from sqldoc.logs import classify_critical, read_error_log, collect_logs
from sqldoc.logs_renderer import build_logs_json, render_logs_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


# --- classification ---------------------------------------------------------

def test_classify_critical_categories():
    assert classify_critical("Error: 823, Severity: 24 ...") == "corruption"
    assert classify_critical("was deadlocked on lock resources") == "deadlock"
    assert classify_critical("There is insufficient memory to run this query") == "memory-pressure"
    assert classify_critical("Could not allocate space for object; disk is full") == "disk-full"
    assert classify_critical("Login failed for user 'sa'. Error: 18456") == "login-failure"
    assert classify_critical("This is an informational message only") == ""


# --- reading + parsing ------------------------------------------------------

def test_read_error_log_parses_severity_and_category(fake_errorlog_rows):
    entries = read_error_log(FakeConnection(fake_errorlog_rows).cursor())
    assert len(entries) == 4
    by_cat = {e.critical for e in entries}
    assert "corruption" in by_cat and "deadlock" in by_cat and "login-failure" in by_cat
    corruption = next(e for e in entries if e.critical == "corruption")
    assert corruption.severity == 24 and corruption.error_number == 823


def test_collect_logs_severity_filter(fake_errorlog_rows):
    report = collect_logs(FakeAdapter(FakeConnection(fake_errorlog_rows)), severity=17)
    # only the Severity 24 corruption line clears the >=17 filter
    assert len(report.entries) == 1
    assert report.entries[0].severity == 24


def test_collect_logs_no_filter(fake_errorlog_rows):
    report = collect_logs(FakeAdapter(FakeConnection(fake_errorlog_rows)))
    assert len(report.entries) == 4
    s = logs.summarize(report)
    assert s["critical"] == 3                 # corruption + deadlock + login-failure
    assert s["max_severity"] == 24
    assert s["by_category"]["corruption"] == 1


def test_collect_logs_degrades(monkeypatch, fake_errorlog_rows):
    monkeypatch.setattr(logs, "read_error_log",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("no EXEC")))
    report = collect_logs(FakeAdapter(FakeConnection(fake_errorlog_rows)))
    assert report.entries == []
    assert report.errors and "ERRORLOG" in report.errors[0][0]


# --- render + json ----------------------------------------------------------

def test_build_and_render(fake_errorlog_rows, tmp_path):
    report = collect_logs(FakeAdapter(FakeConnection(fake_errorlog_rows)))
    data = build_logs_json("PRODSQL01", report)
    assert data["report_type"] == "logs"
    assert data["summary"]["critical"] == 3
    assert any(e["critical"] == "corruption" for e in data["entries"])

    out = tmp_path / "logs.html"
    render_logs_html("PRODSQL01", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Error Log" in h and "corruption" in h and "deadlock" in h


# --- CLI --------------------------------------------------------------------

def test_logs_cli(monkeypatch, fake_errorlog_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_errorlog_rows)))
    out = tmp_path / "logs.html"
    jout = tmp_path / "logs.json"
    res = CliRunner().invoke(cli.cli, [
        "logs", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Entries: 4" in res.output
    assert "Critical: 3" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["report_type"] == "logs"


def test_logs_cli_severity_filter(monkeypatch, fake_errorlog_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_errorlog_rows)))
    res = CliRunner().invoke(cli.cli, [
        "logs", "--server", "h", "--username", "u", "--password", "p",
        "--severity", "17", "--output", str(tmp_path / "logs.html"),
    ])
    assert res.exit_code == 0, res.output
    assert "Entries: 1" in res.output
