"""Access review: SoD, over-privilege, orphaned, inactive, service accounts."""
from datetime import datetime, timezone

import pytest

from sqldoc.access import review as review_mod
from sqldoc.access.review import review_access, review_database, review_logins
from sqldoc.access.model import ADUser
from sqldoc.access.render import build_review_json, render_review_html
from sqldoc.access.titles import expected_level_for_title, is_service_account, exceeds


NOW = datetime(2026, 7, 12, tzinfo=timezone.utc).timestamp()


class FakeCursor:
    def __init__(self, data):
        self.data, self._rows = data, []

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
    def __init__(self, data):
        self._data = data

    def connect(self):
        return FakeConn(self._data)

    def cursor(self, conn):
        return conn.cursor()


class FakeSource:
    def get_user(self, name):
        part = name.split("\\")[-1].lower()
        if part == "ghost":
            return ADUser(identifier=name, found=False)
        if part == "jsmith":
            return ADUser(identifier=name, found=True, title="Analyst", department="Sales")
        if part == "bob":
            return ADUser(identifier=name, found=True, title="Data Engineer")
        return ADUser(identifier=name, found=True, title="")


DATA = {
    "ACCESS_SERVER_LOGINS": [
        {"name": "CORP\\ghost", "type_desc": "WINDOWS_LOGIN", "is_disabled": 0},
        {"name": "CORP\\jsmith", "type_desc": "WINDOWS_LOGIN", "is_disabled": 0},
        {"name": "svc_etl", "type_desc": "SQL_LOGIN", "is_disabled": 0},
    ],
    "ACCESS_SERVER_ROLE_MEMBERS": [],
    "ACCESS_LOGIN_ACTIVITY": [
        {"name": "CORP\\jsmith", "last_activity": "2025-01-01T00:00:00+00:00"},
    ],
    "ACCESS_DB_PRINCIPALS": [
        {"db_user": "CORP\\Bob", "type_desc": "WINDOWS_USER"},
        {"db_user": "svc_etl", "type_desc": "SQL_USER"},
        {"db_user": "CORP\\jsmith", "type_desc": "WINDOWS_USER"},
    ],
    "ACCESS_DB_ROLE_MEMBERS": [
        {"role_name": "db_datawriter", "member_name": "CORP\\Bob"},
        {"role_name": "db_securityadmin", "member_name": "CORP\\Bob"},
        {"role_name": "db_owner", "member_name": "svc_etl"},
        {"role_name": "db_owner", "member_name": "CORP\\jsmith"},
    ],
    "ACCESS_DB_PERMISSIONS": [],
}


# --- title heuristics ------------------------------------------------------

def test_expected_level_for_title():
    assert expected_level_for_title("Senior DBA") == "admin"
    assert expected_level_for_title("Data Engineer") == "write"
    assert expected_level_for_title("Business Analyst") == "read"
    assert expected_level_for_title("") == "read"


def test_is_service_account():
    assert is_service_account("svc_etl") and is_service_account("CORP\\WEBSRV$")
    assert is_service_account("app-pool-1") and not is_service_account("CORP\\jsmith")


def test_exceeds():
    assert exceeds("admin", "read", by=2) and not exceeds("write", "read", by=2)


# --- per-check -------------------------------------------------------------

def test_review_database_flags_sod_service_and_overpriv():
    findings = review_database(FakeCursor(DATA), "prod", "Sales", FakeSource())
    cats = {f.category for f in findings}
    assert "sod" in cats and "service_account" in cats and "over_privileged" in cats
    sod = next(f for f in findings if f.category == "sod")
    assert sod.principal == "CORP\\Bob" and "DROP MEMBER" in sod.fix_sql
    svc = next(f for f in findings if f.category == "service_account")
    assert svc.principal == "svc_etl"


def test_review_logins_orphaned_and_inactive():
    findings = review_logins(FakeCursor(DATA), "prod", FakeSource(), inactive_days=90, now_epoch=NOW)
    cats = {f.category for f in findings}
    assert "orphaned" in cats and "inactive" in cats
    orphan = next(f for f in findings if f.category == "orphaned")
    assert orphan.principal == "CORP\\ghost" and "DROP LOGIN" in orphan.fix_sql
    inactive = next(f for f in findings if f.category == "inactive")
    assert inactive.principal == "CORP\\jsmith" and "DISABLE" in inactive.fix_sql


def test_review_logins_no_source_skips_orphan():
    findings = review_logins(FakeCursor(DATA), "prod", None, inactive_days=90, now_epoch=NOW)
    assert not any(f.category == "orphaned" for f in findings)


# --- orchestrator ----------------------------------------------------------

def _cfg():
    return {"access": {"ad": {"type": "ldap", "server": "x", "base_dn": "y"},
                       "servers": [{"name": "prod", "connection_string": "c",
                                    "dialect": "sqlserver", "databases": ["Sales"]}]}}


def test_review_access_end_to_end():
    findings = review_access(_cfg(), source=FakeSource(),
                             adapter_factory=lambda e, d: FakeAdapter(DATA),
                             inactive_days=90, now_epoch=NOW)
    cats = {f.category for f in findings}
    assert {"sod", "service_account", "over_privileged", "orphaned", "inactive"} <= cats
    # sorted most-severe first
    assert findings[0].severity == "HIGH"


def test_review_access_isolates_errors():
    def boom(e, d):
        raise RuntimeError("no connect")
    findings = review_access(_cfg(), source=FakeSource(), adapter_factory=boom, now_epoch=NOW)
    assert any(f.category == "error" for f in findings)


# --- render ----------------------------------------------------------------

def test_build_review_json():
    findings = review_access(_cfg(), source=FakeSource(),
                             adapter_factory=lambda e, d: FakeAdapter(DATA), now_epoch=NOW)
    j = build_review_json(findings)
    assert j["report_type"] == "access-review" and j["total"] == len(findings)
    assert j["by_severity"]["HIGH"] >= 3


def test_render_review_html_offline(tmp_path):
    from sqldoc.offline import verify_file
    findings = review_access(_cfg(), source=FakeSource(),
                             adapter_factory=lambda e, d: FakeAdapter(DATA), now_epoch=NOW)
    out = tmp_path / "review.html"
    render_review_html(findings, str(out))
    text = out.read_text(encoding="utf-8")
    assert "Access review" in text and "Separation-of-duties" in text
    assert verify_file(str(out)) == []


# --- CLI -------------------------------------------------------------------

def test_cli_access_review(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    monkeypatch.setattr(review_mod, "review_access",
                        lambda cfg, **k: review_access(cfg, source=FakeSource(),
                                                       adapter_factory=lambda e, d: FakeAdapter(DATA),
                                                       now_epoch=NOW))
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(_cfg()), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["access", "review", "--config", str(p),
                                       "--output", str(tmp_path / "rev.html")])
    assert res.exit_code == 0, res.output
    assert "HIGH" in res.output and "sod" in res.output
