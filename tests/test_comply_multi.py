"""Multi-database board-level access report: aggregation, render, CLI."""
import json

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.comply import Permission
from sqldoc.comply_multi import (collect_database_access, build_cross_db,
                                 summarize_multi, DatabaseAccess)
from sqldoc.comply_multi_renderer import build_multi_comply_json, render_multi_comply_html
from sqldoc.pii import scan_tables
from sqldoc.extractor import Table, Column
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection


def _table(name, cols, schema="dbo"):
    return Table(schema, name, 1, columns=cols)


def _c(name, dt="nvarchar"):
    return Column(name, dt, 50, True, False, False, None, None)


def _people_findings():
    return scan_tables([_table("People", [_c("NationalID")])])   # dbo.People HIGH PII


# --- aggregation ------------------------------------------------------------

def test_build_cross_db_matrix():
    findings = _people_findings()
    perms = [
        Permission("app_reader", "SQL_USER", "SELECT", "GRANT", "dbo", "People", "USER_TABLE"),
        Permission("dba", "SQL_USER", "CONTROL", "GRANT", "dbo", "People", "USER_TABLE"),
    ]
    sales = collect_database_access("Sales", findings, perms)
    # HR: only app_reader, and only INSERT (write)
    hr = collect_database_access("HR", findings, [
        Permission("app_reader", "SQL_USER", "INSERT", "GRANT", "dbo", "People", "USER_TABLE"),
    ])
    report = build_cross_db([sales, hr])

    assert report.databases == ["Sales", "HR"]
    by_name = {p.principal: p for p in report.principals}

    reader = by_name["app_reader"]
    assert reader.database_count == 2                 # present in both DBs
    assert "read" in reader.levels and "write" in reader.levels
    assert reader.max_risk == "HIGH"
    assert set(reader.per_db) == {"Sales", "HR"}

    dba = by_name["dba"]
    assert dba.database_count == 1                    # only Sales
    assert dba.levels == ["admin"]

    # cross-DB principal sorts first (broadest reach)
    assert report.principals[0].principal == "app_reader"

    s = summarize_multi(report)
    assert s["databases"] == 2
    assert s["cross_db_principals"] == 1              # app_reader
    assert s["high_risk_principals"] >= 1


def test_build_cross_db_records_errors():
    report = build_cross_db([
        DatabaseAccess(database="Broken", error="PermissionError: denied"),
        collect_database_access("Ok", _people_findings(), [
            Permission("u", "SQL_USER", "SELECT", "GRANT", "dbo", "People", "USER_TABLE")]),
    ])
    assert ("Broken", "PermissionError: denied") in report.errors


# --- render + json ----------------------------------------------------------

def test_render_and_json(tmp_path):
    findings = _people_findings()
    perms = [Permission("app_reader", "SQL_USER", "SELECT", "GRANT", "dbo", "People", "USER_TABLE")]
    report = build_cross_db([
        collect_database_access("Sales", findings, perms),
        collect_database_access("HR", findings, perms),
    ])

    data = build_multi_comply_json(report)
    assert data["report_type"] == "compliance-multi"
    assert data["databases"] == ["Sales", "HR"]
    p = next(x for x in data["principals"] if x["principal"] == "app_reader")
    assert p["database_count"] == 2
    assert p["per_database"]["Sales"]["levels"] == ["read"]

    out = tmp_path / "multi.html"
    render_multi_comply_html(report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Access across 2 databases" in h
    assert "app_reader" in h and "Sales" in h and "HR" in h


# --- CLI --------------------------------------------------------------------

def test_comply_all_databases_cli(monkeypatch, fake_permission_rows, tmp_path):
    people = _table("People", [
        Column("Id", "int", 4, False, True, False, None, None),
        _c("NationalID"),
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda adapter: [people])
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_permission_rows)))

    ymlpath = tmp_path / ".sqldoc.yml"
    ymlpath.write_text(
        "databases:\n"
        "  - name: Sales\n"
        "    connection_string: \"Driver=x;Server=s1;Database=Sales;\"\n"
        "    dialect: sqlserver\n"
        "  - name: HR\n"
        "    connection_string: \"Driver=x;Server=s2;Database=HR;\"\n"
        "    dialect: sqlserver\n",
        encoding="utf-8")
    out = tmp_path / "multi.html"
    jout = tmp_path / "multi.json"
    res = CliRunner().invoke(cli.cli, [
        "comply", "--all-databases", "--config", str(ymlpath),
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Cross-database compliance report" in res.output
    assert "Databases: 2" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["report_type"] == "compliance-multi"
    assert data["databases"] == ["Sales", "HR"]
    # app_reader (from the shared fake grants) has access in both databases
    assert any(p["principal"] == "app_reader" and p["database_count"] == 2
               for p in data["principals"])


def test_comply_all_databases_requires_config(tmp_path):
    ymlpath = tmp_path / ".sqldoc.yml"
    ymlpath.write_text("output: x.html\n", encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["comply", "--all-databases", "--config", str(ymlpath)])
    assert res.exit_code != 0
    assert "databases:" in res.output
