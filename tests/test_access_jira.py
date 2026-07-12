"""Jira access-ticket workflow: connector read/comment/transition + orchestration."""
import pytest

from sqldoc.integrations import jira as jira_mod
from sqldoc.integrations.jira import adf_to_text, adf_from_blocks, Client
from sqldoc.access import jira_flow
from sqldoc.access.jira_flow import extract_request, process_ticket, build_comment_blocks
from sqldoc.access.model import AccessReport, ADUser, DatabaseAccess, Login
from sqldoc.extractor import Table, Column


JIRA_CONFIG = {"base_url": "https://acme.atlassian.net", "email": "bot@acme.com",
               "api_token": "tok", "project_key": "SEC"}


# --- ADF helpers -----------------------------------------------------------

def test_adf_to_text_flattens():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Please grant "},
                                          {"type": "text", "text": "jsmith read on Sales."}]}]}
    assert "jsmith read on Sales." in adf_to_text(adf)


def test_adf_to_text_plain_string():
    assert adf_to_text("legacy v2 text") == "legacy v2 text"


def test_adf_from_blocks_code():
    doc = adf_from_blocks([("h", "Script"), ("code", "ALTER ROLE ...;"), ("p", "run it")])
    kinds = [c["type"] for c in doc["content"]]
    assert "heading" in kinds and "codeBlock" in kinds and "paragraph" in kinds


# --- connector methods (jira_request mocked) -------------------------------

def make_router(transitions=None):
    calls = []

    def router(method, path, cfg, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/transitions") and method == "GET":
            return {"transitions": transitions or [{"id": "31", "name": "Done",
                                                    "to": {"name": "Done"}}]}
        if "/issue/" in path and method == "GET" and "/transitions" not in path and "/comment" not in path:
            return {"key": "SEC-1", "fields": {
                "summary": "Access request",
                "description": "Please grant jsmith@corp.com write access to the Sales database.",
                "reporter": {"displayName": "Boss"}}}
        return {"id": "10001"}

    router.calls = calls
    return router


def test_get_issue(monkeypatch):
    monkeypatch.setattr(jira_mod, "jira_request", make_router())
    issue = Client(JIRA_CONFIG).get_issue("SEC-1")
    assert issue["fields"]["summary"] == "Access request"


def test_add_comment(monkeypatch):
    router = make_router()
    monkeypatch.setattr(jira_mod, "jira_request", router)
    Client(JIRA_CONFIG).add_comment("SEC-1", adf_from_blocks([("p", "hi")]))
    assert any("/comment" in c[1] for c in router.calls)


def test_transition_found(monkeypatch):
    router = make_router()
    monkeypatch.setattr(jira_mod, "jira_request", router)
    assert Client(JIRA_CONFIG).transition("SEC-1", "Done") is True
    posts = [c for c in router.calls if c[1].endswith("/transitions") and c[0] == "POST"]
    assert posts and posts[0][2]["json"]["transition"]["id"] == "31"


def test_transition_not_available(monkeypatch):
    monkeypatch.setattr(jira_mod, "jira_request", make_router())
    assert Client(JIRA_CONFIG).transition("SEC-1", "Nonexistent") is False


# --- extraction ------------------------------------------------------------

def test_extract_request_ai(monkeypatch):
    import sqldoc.ai as real_ai
    monkeypatch.setattr(real_ai, "dispatch", lambda *a, **k:
        '{"user":"jsmith@corp.com","database":"Sales","level":"write","justification":"new hire"}')
    req = extract_request("Access request", "grant write to Sales", known_databases=["Sales"])
    assert req.user == "jsmith@corp.com" and req.database == "Sales" and req.level == "write"
    assert req.justification == "new hire"


def test_extract_request_heuristic():
    req = extract_request("Access", "Please grant jsmith@corp.com read on Sales",
                          known_databases=["Sales"], no_ai=True)
    assert req.user == "jsmith@corp.com" and req.database == "Sales" and req.level == "read"


# --- full workflow ---------------------------------------------------------

class FakeJira:
    def __init__(self):
        self.commented = None
        self.transitioned_to = None

    def get_issue(self, key):
        return {"key": key, "fields": {
            "summary": "Access request",
            "description": "Please grant jsmith@corp.com write access to the Sales database."}}

    def add_comment(self, key, adf):
        self.commented = adf

    def transition(self, key, name):
        self.transitioned_to = name
        return True


def _cfg():
    return {"access": {"ad": {"type": "ldap", "server": "x", "base_dn": "y"},
                       "servers": [{"name": "prod", "connection_string": "c",
                                    "dialect": "sqlserver", "databases": ["Sales"]}]}}


def _report_none():
    return AccessReport(user=ADUser(identifier="jsmith@corp.com", display_name="Jane Smith",
                                    login="CORP\\jsmith", groups=["Sales Team"], found=True))


def test_process_ticket_posts_script(monkeypatch):
    monkeypatch.setattr(jira_flow, "check_access", lambda cfg, u, **k: _report_none(), raising=False)
    # patch the names actually used inside process_ticket (imported locally)
    from sqldoc.access import checker
    monkeypatch.setattr(checker, "check_access", lambda cfg, u, **k: _report_none())
    monkeypatch.setattr(jira_flow, "_tables_for",
                        lambda cfg, db: ([Table("Sales", "Customer", 1, [
                            Column("Email", "varchar", 50, True, False, False, None, None)])], [], "prod"))
    fake = FakeJira()
    result = process_ticket(_cfg(), "SEC-1", fake, transition_to="Done", no_ai=True)
    assert result.extracted.user == "jsmith@corp.com"
    assert result.gap.verdict == "NONE"       # no current access
    assert result.script and "ALTER ROLE" in result.script.grant_sql
    assert result.comment_posted and fake.commented is not None
    assert result.transitioned and fake.transitioned_to == "Done"


def test_process_ticket_unknown_user_comments_note(monkeypatch):
    fake = FakeJira()
    fake.get_issue = lambda key: {"key": key, "fields": {"summary": "help",
                                                         "description": "we need more access somewhere"}}
    result = process_ticket(_cfg(), "SEC-2", fake, no_ai=True)
    assert not result.gap and "Could not determine" in result.note
    assert result.comment_posted


def test_build_comment_blocks_has_code():
    from sqldoc.access.parse import parse_request
    from sqldoc.access.gap import analyze_gap
    from sqldoc.access.script import generate_script
    from sqldoc.access.jira_flow import TicketResult, TicketRequest
    from sqldoc.access.model import ParsedRequest
    report = _report_none()
    report.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    parsed = ParsedRequest(raw="write Sales", database="Sales", level="write")
    gap = analyze_gap(parsed, report)
    gs = generate_script(report, parsed, "prod", "Sales", tables=[], pii_findings=[])
    result = TicketResult(ticket="SEC-1", extracted=TicketRequest(user="jsmith", database="Sales",
                                                                  level="write"),
                          gap=gap, script=gs)
    blocks = build_comment_blocks(result)
    assert any(k == "code" for k, _ in blocks)
    assert any("Execution instructions" in str(t) for _, t in blocks)


# --- CLI -------------------------------------------------------------------

def test_cli_access_jira(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.access import checker
    monkeypatch.setattr(checker, "check_access", lambda cfg, u, **k: _report_none())
    monkeypatch.setattr(jira_flow, "_tables_for", lambda cfg, db: ([], [], "prod"))
    monkeypatch.setattr("sqldoc.integrations.get_client", lambda name, conf: FakeJira())
    cfg = {**_cfg(), "jira": JIRA_CONFIG}
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["access", "jira", "--config", str(p), "--ticket", "SEC-1",
                                       "--no-ai", "--output", str(tmp_path / "j.html")])
    assert res.exit_code == 0, res.output
    assert "jsmith@corp.com" in res.output and "NONE" in res.output
