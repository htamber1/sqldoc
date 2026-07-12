"""Access-request intake: email parsing, ServiceNow/ADO/GitHub fetchers, the
shared workflow, and the REST intake endpoint. All transports mocked."""
import pytest

from sqldoc.access import intake
from sqldoc.access.intake import parse_email, process_item, run_request, IntakeItem
from sqldoc.access.model import AccessReport, ADUser, Login
from sqldoc.extractor import Table, Column


def _cfg():
    return {"access": {"ad": {"type": "native"},
                       "servers": [{"name": "prod", "connection_string": "c",
                                    "dialect": "sqlserver", "databases": ["Sales"]}],
                       "intake": {"github": {"repo": "acme/db", "label": "access-request"}}},
            "servicenow": {"instance_url": "https://acme.service-now.com", "username": "u",
                           "password": "p"},
            "azuredevops": {"organization": "acme", "project": "Data", "pat": "t"}}


def _report():
    r = AccessReport(user=ADUser(identifier="jsmith@corp.com", login="CORP\\jsmith",
                                 groups=["Sales Team"], found=True))
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    return r


@pytest.fixture(autouse=True)
def _patch_workflow(monkeypatch):
    from sqldoc.access import checker
    monkeypatch.setattr(checker, "check_access", lambda cfg, u, **k: _report())
    monkeypatch.setattr(intake, "_tables_for",
                        lambda cfg, db: ([Table("Sales", "Customer", 1, [
                            Column("Email", "varchar", 50, True, False, False, None, None)])], [], "prod", "sqlserver"))


# --- email -----------------------------------------------------------------

def test_parse_email_plain():
    raw = ("From: Jane Smith <jsmith@corp.com>\nSubject: Access please\n\n"
           "Please grant jsmith@corp.com write access to the Sales database.")
    item = parse_email(raw)
    assert item.source == "email" and item.title == "Access please"
    assert "Sales" in item.body and item.requester_hint == "jsmith@corp.com"


def test_parse_email_multipart():
    raw = ("From: a@b.com\nSubject: Req\nMIME-Version: 1.0\n"
           "Content-Type: multipart/alternative; boundary=X\n\n"
           "--X\nContent-Type: text/plain\n\nread access to Sales\n--X--\n")
    item = parse_email(raw)
    assert "read access to Sales" in item.body


def test_process_item_email(monkeypatch):
    item = parse_email("From: jsmith@corp.com\nSubject: r\n\nwrite access to the Sales database")
    outcome = process_item(_cfg(), item, no_ai=True)
    assert outcome.extracted.user == "jsmith@corp.com"
    assert outcome.gap is not None and outcome.script is not None
    assert "ALTER ROLE" in outcome.script.grant_sql


def test_process_item_missing_info():
    item = IntakeItem(source="email", title="help", body="we need something")
    outcome = process_item(_cfg(), item, no_ai=True)
    assert outcome.script is None and "Could not determine" in outcome.note


# --- run_request -----------------------------------------------------------

def test_run_request():
    outcome = run_request(_cfg(), "jsmith@corp.com", "Sales", "write")
    assert outcome.gap.verdict in ("NONE", "PARTIAL", "ALREADY")
    assert outcome.script is not None


# --- ServiceNow fetcher ----------------------------------------------------

def test_from_servicenow(monkeypatch):
    from sqldoc.integrations import servicenow as sn
    monkeypatch.setattr(sn, "sn_request", lambda m, p, cfg, **k: {"result": [
        {"number": "REQ001", "short_description": "Need Sales read",
         "description": "grant jsmith@corp.com read on Sales",
         "requested_for": {"display_value": "jsmith@corp.com"}}]})
    items = intake.from_servicenow(_cfg())
    assert len(items) == 1 and items[0].id == "REQ001"
    assert items[0].requester_hint == "jsmith@corp.com"


# --- Azure DevOps fetcher --------------------------------------------------

def test_from_azuredevops(monkeypatch):
    from sqldoc.integrations import azuredevops as ado

    def fake(method, url, cfg, **k):
        if "/wiql" in url:
            return {"workItems": [{"id": 42}]}
        return {"fields": {"System.Title": "Access for Sales",
                           "System.Description": "<div>write access to Sales for jsmith@corp.com</div>",
                           "System.CreatedBy": {"uniqueName": "jsmith@corp.com"}}}
    monkeypatch.setattr(ado, "ado_request", fake)
    items = intake.from_azuredevops(_cfg())
    assert len(items) == 1 and items[0].id == "42"
    assert "write access" in items[0].body and "<div>" not in items[0].body


# --- GitHub fetcher --------------------------------------------------------

def test_from_github(monkeypatch):
    monkeypatch.setattr(intake, "github_request", lambda m, p, cfg, **k: [
        {"number": 7, "title": "Sales access", "body": "read access to Sales",
         "user": {"login": "octocat"}, "html_url": "https://gh/7"},
        {"number": 8, "pull_request": {}, "title": "a PR"}])   # PRs skipped
    items = intake.from_github(_cfg())
    assert len(items) == 1 and items[0].id == "7" and items[0].requester_hint == "octocat"


def test_from_github_needs_repo():
    from sqldoc.integrations.base import IntegrationError
    with pytest.raises(IntegrationError):
        intake.from_github({"access": {}})


# --- REST endpoint ---------------------------------------------------------

def test_rest_access_request_endpoint():
    from sqldoc.api import _ep_access_request
    ctx = {"config": _cfg(), "mode": "local"}
    out = _ep_access_request(None, ctx, {}, {"user": "jsmith@corp.com",
                                             "database": "Sales", "level": "write"})
    assert out["user"] == "jsmith@corp.com" and out["verdict"] in ("NONE", "PARTIAL", "ALREADY")
    assert "grant_sql" in out["script"]


def test_rest_access_request_needs_user():
    from sqldoc.api import _ep_access_request
    with pytest.raises(ValueError):
        _ep_access_request(None, {"config": _cfg()}, {}, {"database": "Sales"})


def test_rest_endpoint_registered():
    from sqldoc.api import ENDPOINTS, _NO_ADAPTER
    assert ("POST", "/api/access/request") in ENDPOINTS
    assert ("POST", "/api/access/request") in _NO_ADAPTER


# --- CLI parse-email -------------------------------------------------------

def test_cli_parse_email(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(_cfg()), encoding="utf-8")
    email = tmp_path / "req.eml"
    email.write_text("From: jsmith@corp.com\nSubject: Access\n\nwrite access to the Sales database",
                     encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["access", "parse-email", "--config", str(p),
                                       "--file", str(email), "--no-ai",
                                       "--output", str(tmp_path / "e.html")])
    assert res.exit_code == 0, res.output
    assert "jsmith@corp.com" in res.output
