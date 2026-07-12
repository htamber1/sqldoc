"""Backup / PITR monitoring across dialects, server integration, agent alert."""
import json

from click.testing import CliRunner

from sqldoc import backup, cli
from sqldoc.backup import collect_backups, summarize, stale_databases
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


# --- SQL Server -------------------------------------------------------------

def test_sqlserver_backups(fake_backup_rows):
    report = collect_backups(FakeAdapter(FakeConnection(fake_backup_rows), dialect="sqlserver"))
    assert report.dialect == "sqlserver" and report.pitr_mechanism == "log backups"
    dbs = {d.database: d for d in report.databases}
    assert dbs["Staging"].never_backed_up
    # FULL recovery with no log backup -> flagged
    assert any("log backups" in i for i in dbs["AdventureWorks2022"].issues)
    # SIMPLE recovery -> no PITR
    assert any("SIMPLE" in i for i in dbs["Sales"].issues)
    assert report.pitr_enabled                       # at least one FULL db

    s = summarize(report)
    assert s["databases"] == 3 and s["never_backed_up"] == 1 and s["with_issues"] == 3


def test_stale_databases(fake_backup_rows):
    report = collect_backups(FakeAdapter(FakeConnection(fake_backup_rows), dialect="sqlserver"))
    stale = stale_databases(report, max_age_hours=24)
    # Staging (never) + Sales (30h > 24h). AdventureWorks (6h) is fresh.
    names = {d.database for d in stale}
    assert "Staging" in names and "Sales" in names and "AdventureWorks2022" not in names


# --- PostgreSQL -------------------------------------------------------------

def test_postgres_backups(fake_pg_backup_rows):
    report = collect_backups(FakeAdapter(FakeConnection(fake_pg_backup_rows), dialect="postgres"))
    assert report.pitr_enabled is True               # archive_mode = on
    assert report.pitr_mechanism == "WAL archiving"
    assert report.archiver["archived_count"] == 1200
    assert [d.database for d in report.databases] == ["pagila", "analytics"]
    assert all(d.pitr_capable for d in report.databases)


def test_postgres_backups_archiving_off():
    rows = {"pgarchmode": [__import__("conftest").FakeRow(setting="off")],
            "pgarchiver": [], "pgdatabases": [__import__("conftest").FakeRow(datname="db1")]}
    report = collect_backups(FakeAdapter(FakeConnection(rows), dialect="postgres"))
    assert not report.pitr_enabled
    assert any("WAL archiving" in n for n in report.notes)
    assert report.databases[0].issues                # no PITR issue flagged


# --- MySQL ------------------------------------------------------------------

def test_mysql_backups(fake_mysql_backup_rows):
    report = collect_backups(FakeAdapter(FakeConnection(fake_mysql_backup_rows), dialect="mysql"))
    assert report.pitr_enabled is True               # log_bin = 1
    assert report.pitr_mechanism == "binary logging"
    assert [d.database for d in report.databases] == ["sakila", "app"]


def test_unsupported_dialect():
    report = collect_backups(FakeAdapter(FakeConnection({}), dialect="sqlite"))
    assert not report.supported and report.databases == []


# --- server integration + JSON ---------------------------------------------

def test_server_includes_backups(fake_server_rows, fake_backup_rows):
    from sqldoc.server import collect_server, summarize as server_summarize
    from sqldoc.server_renderer import build_server_json
    combined = {**fake_server_rows, **fake_backup_rows}
    report = collect_server(FakeAdapter(FakeConnection(combined), dialect="sqlserver"),
                            include_jobs=True, include_backups=True)
    assert report.backups is not None and len(report.backups.databases) == 3
    s = server_summarize(report)
    assert s["never_backed_up"] == 1 and s["backup_issues"] == 3
    data = build_server_json("SRV", report)
    assert data["backups"]["pitr_mechanism"] == "log backups"
    assert any(d["never_backed_up"] for d in data["backups"]["databases"])


def test_server_cli_backups(monkeypatch, fake_server_rows, fake_backup_rows, tmp_path):
    combined = {**fake_server_rows, **fake_backup_rows}
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(combined)))
    out = tmp_path / "server.html"
    res = CliRunner().invoke(cli.cli, [
        "server", "--server", "h", "--username", "u", "--password", "p", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert "Backups:" in res.output and "Never backed up: 1" in res.output
    h = out.read_text(encoding="utf-8")
    assert "point-in-time recovery" in h and "Staging" in h
