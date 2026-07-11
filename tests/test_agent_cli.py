"""`sqldoc agent` CLI: process helpers, status/logs, and a real end-to-end run
against a temp SQLite database (no network, no AI, no subprocess)."""
import os
import sqlite3
import threading
import time

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.agent import db_path, log_path, pid_path
from sqldoc.agent.cli import pid_alive, _read_pid, _write_pid


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False
    assert pid_alive(None) is False
    assert pid_alive(2_000_000_000) is False    # almost certainly not a live pid


def test_pid_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLDOC_AGENT_HOME", str(tmp_path))
    assert _read_pid() is None
    _write_pid(12345)
    assert _read_pid() == 12345


def test_agent_group_lists_subcommands():
    res = CliRunner().invoke(cli.cli, ["agent", "--help"])
    assert res.exit_code == 0
    for sub in ("start", "stop", "status", "logs"):
        assert sub in res.output


def test_status_when_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLDOC_AGENT_HOME", str(tmp_path))
    res = CliRunner().invoke(cli.cli, ["agent", "status", "--config", "does-not-exist.yml"])
    assert res.exit_code == 0
    assert "stopped" in res.output
    assert "No monitored databases yet" in res.output


def test_stop_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLDOC_AGENT_HOME", str(tmp_path))
    res = CliRunner().invoke(cli.cli, ["agent", "stop"])
    assert res.exit_code == 0
    assert "not running" in res.output


def test_logs_empty_and_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLDOC_AGENT_HOME", str(tmp_path))
    assert "No agent log yet" in CliRunner().invoke(cli.cli, ["agent", "logs"]).output
    with open(log_path(), "w", encoding="utf-8") as f:
        f.write("\n".join(f"line {i}" for i in range(60)))
    out = CliRunner().invoke(cli.cli, ["agent", "logs", "-n", "5"]).output
    assert "line 59" in out and "line 10" not in out


def test_start_rejects_bad_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLDOC_AGENT_HOME", str(tmp_path))
    cfg = tmp_path / "bad.yml"
    cfg.write_text("agent:\n  databases: []\n")     # empty -> invalid
    res = CliRunner().invoke(cli.cli, ["agent", "start", "--config", str(cfg)])
    assert res.exit_code != 0
    assert "non-empty list" in res.output


def _make_sqlite(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE person(id INTEGER PRIMARY KEY, email TEXT, full_name TEXT);"
        "INSERT INTO person VALUES (1,'a@b.com','Ann'),(2,'c@d.com','Bob');")
    conn.commit()
    conn.close()


def test_end_to_end_real_sqlite(tmp_path, monkeypatch):
    """Run the real daemon (real extraction + store + dashboard) against a temp
    SQLite DB, then read it back through the CLI status command."""
    monkeypatch.setenv("SQLDOC_AGENT_HOME", str(tmp_path / "home"))
    dbfile = tmp_path / "shop.db"
    _make_sqlite(dbfile)

    from sqldoc.agent.store import AgentStore
    from sqldoc.agent.config import AgentConfig, DatabaseConfig, NotifyConfig
    from sqldoc.agent.daemon import run_daemon
    from sqldoc.agent.notify import Notifier

    store = AgentStore(db_path())
    db = DatabaseConfig(name="shop", connection_string=str(dbfile), dialect="sqlite", no_ai=True)
    ac = AgentConfig(interval_minutes=1, dashboard_port=0, databases=[db], notify=NotifyConfig())
    stop = threading.Event()
    t = threading.Thread(target=lambda: run_daemon(ac, store, Notifier(ac.notify), stop,
                                                   log=lambda *_: None), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if store.latest_metric("shop"):
                break
            time.sleep(0.05)
    finally:
        stop.set()
        t.join(timeout=10)

    m = store.latest_metric("shop")
    assert m and m["tables"] == 1
    assert m["pii_medium"] >= 1                          # email is MEDIUM PII
    html, _ = store.get_doc("shop")
    assert html and "person" in html
    assert store.last_run("shop")["status"] == "ok"

    # the CLI status command surfaces it
    res = CliRunner().invoke(cli.cli, ["agent", "status", "--config", "none.yml"])
    assert "shop" in res.output and "tables=1" in res.output
