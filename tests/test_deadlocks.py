"""Deadlock analysis across dialects: XML graph parsing, SVG, AI, render, CLI."""
import json

from click.testing import CliRunner

from sqldoc import cli, deadlocks as dl_mod
from sqldoc.deadlocks import collect_deadlocks, parse_deadlock_xml, explain_deadlock, summarize
from sqldoc.deadlocks_renderer import build_deadlocks_json, render_deadlocks_html, _layout
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter, _DEADLOCK_XML


# --- SQL Server graph parsing -----------------------------------------------

def test_parse_deadlock_xml():
    events = parse_deadlock_xml(_DEADLOCK_XML)
    assert len(events) == 1
    ev = events[0]
    assert ev.timestamp.startswith("2026-07-11")
    assert ev.victim_id == "process1"
    procs = {p.id: p for p in ev.processes}
    assert procs["process1"].spid == "55" and procs["process1"].is_victim
    assert "UPDATE Orders SET Total=1" in procs["process1"].query
    assert not procs["process2"].is_victim
    # two resources -> mutual wait-for edges
    assert ("process1", "process2") in ev.edges and ("process2", "process1") in ev.edges


def test_collect_sqlserver_deadlocks(fake_mssql_deadlock_rows):
    report = collect_deadlocks(FakeAdapter(FakeConnection(fake_mssql_deadlock_rows), dialect="sqlserver"))
    assert report.total_count == 1 and len(report.events) == 1
    s = summarize(report)
    assert s["graph_events"] == 1


def test_collect_sqlserver_none():
    report = collect_deadlocks(FakeAdapter(FakeConnection({}), dialect="sqlserver"))
    assert report.events == [] and report.notes


# --- PostgreSQL -------------------------------------------------------------

def test_postgres_deadlocks(fake_pg_deadlock_rows):
    report = collect_deadlocks(FakeAdapter(FakeConnection(fake_pg_deadlock_rows), dialect="postgres"))
    assert report.total_count == 7
    assert len(report.events) == 1 and report.events[0].kind == "current-blocking"
    ev = report.events[0]
    assert ev.processes[0].is_victim                  # blocked process is the "victim"
    assert ev.edges == [("100", "101")]


# --- MySQL ------------------------------------------------------------------

def test_mysql_deadlocks(fake_mysql_deadlock_rows):
    report = collect_deadlocks(FakeAdapter(FakeConnection(fake_mysql_deadlock_rows), dialect="mysql"))
    assert report.total_count == 3 and report.events == []
    assert any("ER_LOCK_DEADLOCK" in n for n in report.notes)


def test_unsupported_dialect():
    report = collect_deadlocks(FakeAdapter(FakeConnection({}), dialect="sqlite"))
    assert not report.supported


# --- SVG layout + AI + render + json + CLI ----------------------------------

def test_layout_svg(fake_mssql_deadlock_rows):
    report = collect_deadlocks(FakeAdapter(FakeConnection(fake_mssql_deadlock_rows), dialect="sqlserver"))
    g = _layout(report.events[0])
    assert len(g["nodes"]) == 2 and len(g["edges"]) == 2
    assert any(n["is_victim"] for n in g["nodes"])
    assert all(e["d"].startswith("M ") for e in g["edges"])


def test_explain_deadlock(monkeypatch, fake_mssql_deadlock_rows):
    captured = {}

    def fake_ai(p, m, mo):
        captured["p"] = p
        return "Cyclic lock order; fix ordering."

    monkeypatch.setattr(dl_mod, "_ai_call", fake_ai)
    report = collect_deadlocks(FakeAdapter(FakeConnection(fake_mssql_deadlock_rows), dialect="sqlserver"))
    text = explain_deadlock(report, mode="local")
    assert "Cyclic lock order" in text
    assert "UPDATE Orders" in captured["p"]           # deadlock SQL fed to the model


def test_build_and_render(fake_mssql_deadlock_rows, tmp_path):
    report = collect_deadlocks(FakeAdapter(FakeConnection(fake_mssql_deadlock_rows), dialect="sqlserver"))
    report.ai_explanation = "Cyclic dependency between two updates."
    data = build_deadlocks_json("SRV", report)
    assert data["report_type"] == "deadlocks" and data["total_count"] == 1
    assert data["events"][0]["victim_id"] == "process1"

    out = tmp_path / "dl.html"
    render_deadlocks_html("SRV", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Deadlock Analysis" in h and "<svg" in h and "VICTIM" in h
    assert "spid 55" in h and "AI analysis" in h


def test_deadlocks_cli(monkeypatch, fake_mssql_deadlock_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_mssql_deadlock_rows)))
    monkeypatch.setattr(dl_mod, "_ai_call", lambda p, m, mo: "Fix the lock order.")
    out = tmp_path / "dl.html"
    jout = tmp_path / "dl.json"
    res = CliRunner().invoke(cli.cli, [
        "deadlocks", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Deadlocks recorded: 1" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["ai_explanation"] == "Fix the lock order."
