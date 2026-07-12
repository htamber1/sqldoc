"""Access check: SQL Server probe, cross-reference, checker, render, CLI.
No live DB, no AD — a token-routed fake cursor + a fake AD source drive it."""
import json

import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.access import checker, sqlserver
from sqldoc.access.model import ADUser, Login
from sqldoc.access.render import build_check_json, render_check_html
from sqldoc.extractor import Table, Column


# --- fakes -----------------------------------------------------------------

class FakeCursor:
    def __init__(self, data):
        self.data = data
        self._rows = []

    def execute(self, sql, *a):
        self._rows = []
        for token, rows in self.data.items():
            if token in sql:
                self._rows = rows
                return

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, data):
        self.data = data

    def cursor(self):
        return FakeCursor(self.data)

    def close(self):
        pass


class FakeAdapter:
    dialect = "sqlserver"
    display_name = "SQL Server"

    def __init__(self, tables, cursor_data):
        self._tables = tables
        self._data = cursor_data

    def extract_metadata(self):
        return self._tables

    def connect(self):
        return FakeConn(self._data)

    def cursor(self, conn):
        return conn.cursor()


def _pii_table():
    return Table(schema="Sales", name="Customer", row_count=10, columns=[
        Column("Id", "int", 4, False, True, False, None, None),
        Column("Email", "varchar", 100, True, False, False, None, None),
    ])


CURSOR_DATA = {
    "ACCESS_SERVER_LOGINS": [
        {"name": "CORP\\Sales Read", "type_desc": "WINDOWS_GROUP", "is_disabled": 0},
        {"name": "CORP\\Admins", "type_desc": "WINDOWS_GROUP", "is_disabled": 0},
        {"name": "sa", "type_desc": "SQL_LOGIN", "is_disabled": 0},
    ],
    "ACCESS_SERVER_ROLE_MEMBERS": [
        {"role_name": "sysadmin", "member_name": "CORP\\Admins"},
    ],
    "ACCESS_DB_PRINCIPALS": [
        {"db_user": "CORP\\Sales Read", "type_desc": "WINDOWS_GROUP"},
    ],
    "ACCESS_DB_ROLE_MEMBERS": [
        {"role_name": "db_datareader", "member_name": "CORP\\Sales Read"},
    ],
    "ACCESS_DB_PERMISSIONS": [],
}


def _user(groups=("Sales Read",)):
    return ADUser(identifier="jsmith", display_name="Jane Smith", sam_account_name="jsmith",
                  login="CORP\\jsmith", title="Analyst", department="Sales",
                  groups=list(groups), source="ldap", found=True)


class FakeSource:
    source = "ldap"

    def __init__(self, user):
        self.user = user

    def get_user(self, ident):
        return self.user


# --- probe / cross-reference ----------------------------------------------

def test_collect_server_logins():
    cur = FakeCursor(CURSOR_DATA)
    logins = sqlserver.collect_server_logins(cur)
    admins = next(l for l in logins if l.name == "CORP\\Admins")
    assert admins.server_roles == ["sysadmin"]
    assert any(l.type == "WINDOWS_GROUP" for l in logins)


def test_match_user_logins_by_group():
    logins = sqlserver.collect_server_logins(FakeCursor(CURSOR_DATA))
    matched = sqlserver.match_user_logins(logins, _user())
    assert [l.name for l in matched] == ["CORP\\Sales Read"]


def test_match_user_logins_direct():
    logins = [Login(name="CORP\\jsmith", type="WINDOWS_LOGIN")]
    matched = sqlserver.match_user_logins(logins, _user(groups=[]))
    assert [l.name for l in matched] == ["CORP\\jsmith"]


def test_collect_db_access_reader_sees_pii():
    from sqldoc.pii import scan_tables
    cur = FakeCursor(CURSOR_DATA)
    logins = sqlserver.collect_server_logins(cur)
    matched = sqlserver.match_user_logins(logins, _user())
    pii = scan_tables([_pii_table()])
    access = sqlserver.collect_db_access(cur, "prod", "Sales", matched, pii)
    assert len(access) == 1
    a = access[0]
    assert a.level == "read" and a.roles == ["db_datareader"]
    assert a.pii_tables and a.pii_tables[0][1] == "Customer"


# --- checker ---------------------------------------------------------------

def _cfg():
    return {"access": {
        "ad": {"type": "ldap", "server": "ldap://dc", "base_dn": "DC=corp"},
        "servers": [{"name": "prod", "connection_string": "DRIVER=x;SERVER=s",
                     "dialect": "sqlserver", "databases": ["Sales"]}],
    }}


def test_check_access_end_to_end():
    factory = lambda entry, db: FakeAdapter([_pii_table()], CURSOR_DATA)
    report = checker.check_access(_cfg(), "jsmith", source=FakeSource(_user()),
                                  adapter_factory=factory)
    assert report.user.found
    assert report.matched_groups == ["CORP\\Sales Read"]
    assert len(report.access) == 1 and report.access[0].level == "read"
    assert report.access[0].pii_tables


def test_check_access_user_not_found():
    u = ADUser(identifier="ghost", found=False, source="ldap")
    report = checker.check_access(_cfg(), "ghost", source=FakeSource(u),
                                  adapter_factory=lambda e, d: FakeAdapter([], CURSOR_DATA))
    assert not report.user.found and report.errors


def test_check_access_db_error_is_noted():
    def boom(entry, db):
        raise RuntimeError("cannot connect")
    report = checker.check_access(_cfg(), "jsmith", source=FakeSource(_user()),
                                  adapter_factory=boom)
    assert report.errors and "cannot connect" in report.errors[0][1]


def test_with_database_swaps_catalog():
    assert "DATABASE=HR" in checker._with_database("DRIVER=x;DATABASE=Sales;UID=u", "HR")
    assert "DATABASE=HR" in checker._with_database("DRIVER=x;SERVER=s", "HR")


# --- render ----------------------------------------------------------------

def test_build_check_json():
    report = checker.check_access(_cfg(), "jsmith", source=FakeSource(_user()),
                                  adapter_factory=lambda e, d: FakeAdapter([_pii_table()], CURSOR_DATA))
    j = build_check_json(report)
    assert j["report_type"] == "access-check"
    assert j["user"]["display_name"] == "Jane Smith"
    assert j["access"][0]["level"] == "read"
    assert j["access"][0]["pii_tables"][0]["table"] == "Customer"


def test_render_check_html_offline(tmp_path):
    from sqldoc.offline import verify_file
    report = checker.check_access(_cfg(), "jsmith", source=FakeSource(_user()),
                                  adapter_factory=lambda e, d: FakeAdapter([_pii_table()], CURSOR_DATA))
    out = tmp_path / "check.html"
    render_check_html(report, str(out))
    text = out.read_text(encoding="utf-8")
    assert "Jane Smith" in text and "db_datareader" in text and "Customer" in text
    assert verify_file(str(out)) == []      # air-gap safe


def test_render_not_found(tmp_path):
    u = ADUser(identifier="ghost", found=False, source="ldap")
    from sqldoc.access.model import AccessReport
    out = tmp_path / "nf.html"
    render_check_html(AccessReport(user=u), str(out))
    assert "not found" in out.read_text(encoding="utf-8")


# --- CLI -------------------------------------------------------------------

def test_cli_access_check(monkeypatch, tmp_path):
    import yaml
    orig = checker.check_access
    monkeypatch.setattr("sqldoc.access.checker.check_access",
                        lambda cfg, ident, **k: orig(
                            cfg, ident, source=FakeSource(_user()),
                            adapter_factory=lambda e, d: FakeAdapter([_pii_table()], CURSOR_DATA)))
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(_cfg()), encoding="utf-8")
    out = tmp_path / "check.html"
    jout = tmp_path / "check.json"
    res = CliRunner().invoke(cli.cli, ["access", "check", "--config", str(p),
                                       "--user", "jsmith", "--output", str(out),
                                       "--json", str(jout)])
    assert res.exit_code == 0, res.output
    assert "Jane Smith" in res.output
    assert out.exists() and json.loads(jout.read_text())["report_type"] == "access-check"
