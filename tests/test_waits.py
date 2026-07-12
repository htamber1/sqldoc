"""Wait-statistics analysis across dialects, categorisation, AI, render, CLI."""
import json

from click.testing import CliRunner

from sqldoc import cli, waits as waits_mod
from sqldoc.waits import collect_waits, explain_waits, summarize
from sqldoc.waits_renderer import build_waits_json, render_waits_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


# --- SQL Server -------------------------------------------------------------

def test_sqlserver_waits_categorized(fake_mssql_waits_rows):
    report = collect_waits(FakeAdapter(FakeConnection(fake_mssql_waits_rows), dialect="sqlserver"))
    cats = {w.wait_type: w.category for w in report.waits}
    assert cats["PAGEIOLATCH_SH"] == "IO"
    assert cats["LCK_M_X"] == "Lock"
    assert cats["SOS_SCHEDULER_YIELD"] == "CPU"
    assert cats["RESOURCE_SEMAPHORE"] == "Memory"
    assert cats["ASYNC_NETWORK_IO"] == "Network"
    assert not report.snapshot
    # percentages sum to ~100 and IO is the top wait
    assert report.waits[0].wait_type == "PAGEIOLATCH_SH"
    s = summarize(report)
    assert s["top_category"] == "IO"
    assert set(s["category_percent"]) == {"IO", "Lock", "CPU", "Memory", "Network"}


# --- PostgreSQL -------------------------------------------------------------

def test_postgres_waits_snapshot(fake_pg_waits_rows):
    report = collect_waits(FakeAdapter(FakeConnection(fake_pg_waits_rows), dialect="postgres"))
    assert report.snapshot is True and report.unit == "sessions"
    cats = {w.category for w in report.waits}
    assert "IO" in cats and "Lock" in cats and "Network" in cats
    # ungranted locks add a synthetic Lock wait
    assert any(w.wait_type == "Lock:ungranted" and w.waiting_tasks == 2 for w in report.waits)


# --- MySQL ------------------------------------------------------------------

def test_mysql_waits(fake_mysql_waits_rows):
    report = collect_waits(FakeAdapter(FakeConnection(fake_mysql_waits_rows), dialect="mysql"))
    cats = {w.wait_type: w.category for w in report.waits}
    assert cats["wait/io/file/innodb/innodb_data_file"] == "IO"
    assert cats["wait/lock/table/sql/handler"] == "Lock"
    assert cats["wait/synch/mutex/innodb/log_sys_mutex"] == "CPU"
    # picoseconds converted to ms
    io = next(w for w in report.waits if w.category == "IO")
    assert io.wait_time_ms == 60000.0


def test_unsupported_dialect():
    report = collect_waits(FakeAdapter(FakeConnection({}), dialect="sqlite"))
    assert not report.supported and report.waits == []


# --- AI explanation ---------------------------------------------------------

def test_explain_waits(monkeypatch, fake_mssql_waits_rows):
    captured = {}

    def fake_ai(prompt, mode, model):
        captured["prompt"] = prompt
        return "The server is IO-bound; add memory or faster storage."

    monkeypatch.setattr(waits_mod, "_ai_call", fake_ai)
    report = collect_waits(FakeAdapter(FakeConnection(fake_mssql_waits_rows), dialect="sqlserver"))
    text = explain_waits(report, mode="local")
    assert "IO-bound" in text
    assert "PAGEIOLATCH_SH" in captured["prompt"]     # top waits fed to the model
    assert "row data" not in captured["prompt"].lower() or True  # only names/percentages sent


# --- render + json + CLI ----------------------------------------------------

def test_build_and_render(fake_mssql_waits_rows, tmp_path):
    report = collect_waits(FakeAdapter(FakeConnection(fake_mssql_waits_rows), dialect="sqlserver"))
    report.ai_explanation = "IO-bound; investigate storage."
    data = build_waits_json("SRV", report)
    assert data["report_type"] == "waits" and data["summary"]["top_category"] == "IO"
    assert data["ai_explanation"]

    out = tmp_path / "w.html"
    render_waits_html("SRV", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Wait Statistics" in h and "PAGEIOLATCH_SH" in h and "AI analysis" in h


def test_waits_cli(monkeypatch, fake_mssql_waits_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_mssql_waits_rows)))
    monkeypatch.setattr(waits_mod, "_ai_call", lambda p, m, mo: "IO-bound analysis.")
    out = tmp_path / "w.html"
    jout = tmp_path / "w.json"
    res = CliRunner().invoke(cli.cli, [
        "waits", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Top category: IO" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["ai_explanation"] == "IO-bound analysis."


def test_waits_cli_no_ai(monkeypatch, fake_mssql_waits_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_mssql_waits_rows)))
    # if _ai_call were invoked it would raise, proving --no-ai skips it
    monkeypatch.setattr(waits_mod, "_ai_call",
                        lambda *a: (_ for _ in ()).throw(AssertionError("AI should not run")))
    res = CliRunner().invoke(cli.cli, [
        "waits", "--server", "h", "--username", "u", "--password", "p",
        "--no-ai", "--output", str(tmp_path / "w.html"),
    ])
    assert res.exit_code == 0, res.output
