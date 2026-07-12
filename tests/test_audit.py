"""Audit trail: recording, redaction, querying, export, and the CLI hook."""
import json
import os

from click.testing import CliRunner

from sqldoc import audit, cli


# --- redaction --------------------------------------------------------------

def test_redact_options_hides_secrets():
    opts = audit.redact_options({
        "server": "h", "database": "DB", "password": "hunter2",
        "connection_string": "DRIVER=...;PWD=secret", "api_key": "abc",
        "no_ai": True, "concurrency": 8, "config": ".sqldoc.yml",
        "verify_offline": True, "sample": False, "schemas": None,
    })
    assert opts["password"] == "***redacted***"
    assert opts["connection_string"] == "***redacted***"
    assert opts["api_key"] == "***redacted***"
    assert opts["server"] == "h" and opts["concurrency"] == 8
    # noise + falsy/None dropped
    assert "config" not in opts and "verify_offline" not in opts
    assert "sample" not in opts and "schemas" not in opts


def test_derive_database_from_conn_str():
    assert audit._derive_database({"connection_string": "SERVER=h;DATABASE=Sales;UID=x"}) == "Sales"
    assert audit._derive_database({"database": "Explicit"}) == "Explicit"
    assert audit._derive_database({}) is None


# --- record + read ----------------------------------------------------------

def test_record_writes_jsonl(tmp_path):
    log = tmp_path / "audit.log"
    audit.record("scan", dialect="postgres", database="DB",
                 options={"schemas": "public"}, result="ok",
                 log_path=str(log), to_store=False)
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    e = json.loads(lines[0])
    assert e["command"] == "scan" and e["dialect"] == "postgres"
    assert e["database"] == "DB" and e["result"] == "ok"
    assert e["user"] and e["at"]


def test_record_appends(tmp_path):
    log = tmp_path / "a.log"
    for i in range(3):
        audit.record(f"cmd{i}", log_path=str(log), to_store=False)
    assert len(audit.read_entries(str(log))) == 3


def test_record_never_raises_on_bad_path():
    # a directory that can't be created shouldn't blow up the command
    audit.record("scan", log_path="/\0/bad", to_store=False)


def test_record_command_derives_and_redacts(tmp_path):
    log = tmp_path / "a.log"
    audit.record_command("scan", {
        "dialect": "mysql", "database": "Shop", "password": "x", "sample": True,
    }, result="ok", log_path=str(log), to_store=False)
    e = audit.read_entries(str(log))[0]
    assert e["dialect"] == "mysql" and e["database"] == "Shop"
    assert e["options"]["password"] == "***redacted***"
    assert e["options"]["sample"] is True


# --- query / summarize / export ---------------------------------------------

def _entries():
    return [
        {"at": "2026-07-10T00:00:00+00:00", "command": "scan", "database": "A", "user": "alice", "result": "ok"},
        {"at": "2026-07-11T00:00:00+00:00", "command": "scan", "database": "B", "user": "bob", "result": "error: boom"},
        {"at": "2026-07-12T00:00:00+00:00", "command": "doc", "database": "A", "user": "alice", "result": "ok"},
    ]


def test_query_filters():
    e = _entries()
    assert len(audit.query(e, command="scan")) == 2
    assert len(audit.query(e, database="A")) == 2
    assert len(audit.query(e, user="bob")) == 1
    assert len(audit.query(e, since="2026-07-11T00:00:00+00:00")) == 2


def test_summarize_counts():
    s = audit.summarize(_entries())
    assert s["total"] == 3 and s["errors"] == 1
    assert s["by_command"]["scan"] == 2
    assert s["by_database"]["A"] == 2
    assert s["by_user"]["alice"] == 2


def test_to_csv():
    csv = audit.to_csv(_entries())
    assert "command" in csv.splitlines()[0]
    assert "scan" in csv and "doc" in csv


# --- CLI hook: commands get recorded ---------------------------------------

def test_cli_command_recorded(monkeypatch, tmp_path):
    from sqldoc.extractor import Table, Column
    t = Table("dbo", "People", 2, columns=[
        Column("SSN", "varchar", 11, True, False, False, None, None)])
    monkeypatch.setattr(cli, "extract_metadata", lambda a: [t])
    res = CliRunner().invoke(cli.cli, [
        "scan", "--dialect", "sqlite", "--connection-string", str(tmp_path / "x.db"),
        "--database", "AuditDB", "--no-baseline", "--output", str(tmp_path / "p.html"),
    ])
    assert res.exit_code == 0, res.output
    # the run was recorded to the isolated audit log
    entries = audit.read_entries()
    scans = [e for e in entries if e["command"] == "scan" and e["database"] == "AuditDB"]
    assert scans and scans[-1]["result"] == "ok"
    assert scans[-1]["dialect"] == "sqlite"


def test_cli_audit_command_queries(monkeypatch, tmp_path):
    # seed a couple of runs directly into the isolated log
    log = audit.audit_log_path()
    audit.record("scan", database="X", result="ok", log_path=log, to_store=False)
    audit.record("doc", database="Y", result="error: nope", log_path=log, to_store=False)
    res = CliRunner().invoke(cli.cli, ["audit"])
    assert res.exit_code == 0
    assert "scan" in res.output and "doc" in res.output

    res2 = CliRunner().invoke(cli.cli, ["audit", "--command", "scan"])
    assert "X" in res2.output and "Y" not in res2.output

    res3 = CliRunner().invoke(cli.cli, ["audit", "--summary"])
    assert "Audit trail:" in res3.output


def test_cli_audit_export_csv(tmp_path, monkeypatch):
    log = audit.audit_log_path()
    audit.record("scan", database="X", result="ok", log_path=log, to_store=False)
    out = tmp_path / "trail.csv"
    res = CliRunner().invoke(cli.cli, ["audit", "--export", str(out)])
    assert res.exit_code == 0 and out.exists()
    assert "scan" in out.read_text(encoding="utf-8")
