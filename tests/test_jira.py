"""Jira connector tests — jira_request monkeypatched, no network."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.integrations import jira
from sqldoc.integrations.base import FindingEvent, IntegrationError


CONFIG = {"base_url": "https://acme.atlassian.net/", "email": "bot@acme.com",
          "api_token": "tok", "project_key": "SEC",
          "issue_types": {"pii": "Security", "health": "Bug", "backup": "Task"},
          "report_url": "https://reports/db"}


def make_router(existing_summaries=()):
    calls = []

    def router(method, path, cfg, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/rest/api/3/myself":
            return {"displayName": "sqldoc bot"}
        if path.startswith("/rest/api/3/project/"):
            return {"key": "SEC", "name": "Security"}
        if path == "/rest/api/3/search":
            jql = kwargs["params"]["jql"]
            hit = any(s in jql for s in existing_summaries)
            return {"issues": ([{"key": "SEC-1"}] if hit else [])}
        if path == "/rest/api/3/issue" and method == "POST":
            return {"key": f"SEC-{len(calls)}"}
        return {}

    router.calls = calls
    return router


EVENTS = [
    FindingEvent("pii", "high", "[sqldoc] 2 HIGH-risk PII column(s) in DB",
                 "SSN, DOB", database="DB"),
    FindingEvent("health", "medium", "[sqldoc] Performance score 40/100 below threshold on DB",
                 "slow queries", database="DB"),
]


def test_issue_type_for_defaults_and_override():
    assert jira.issue_type_for(CONFIG, "pii") == "Security"
    assert jira.issue_type_for(CONFIG, "health") == "Bug"
    assert jira.issue_type_for({}, "backup") == "Task"      # default
    assert jira.issue_type_for({}, "unknown_kind") == "Task"


def test_adf_shape():
    doc = jira._adf("line one\nline two")
    assert doc["type"] == "doc" and doc["version"] == 1
    assert len(doc["content"]) == 2


def test_test_ok(monkeypatch):
    monkeypatch.setattr(jira, "jira_request", make_router())
    res = jira.Client(CONFIG).test()
    assert res["ok"] and "sqldoc bot" in res["detail"]


def test_create_issues_routes_types(monkeypatch):
    router = make_router()
    monkeypatch.setattr(jira, "jira_request", router)
    res = jira.Client(CONFIG).create_issues(EVENTS)
    assert res["ok"] and len(res["created"]) == 2
    posts = [c for c in router.calls if c[1] == "/rest/api/3/issue"]
    types = [p[2]["json"]["fields"]["issuetype"]["name"] for p in posts]
    assert "Security" in types and "Bug" in types
    # report link + database appear in the description ADF text
    text = str(posts[0][2]["json"]["fields"]["description"])
    assert "reports/db" in text


def test_create_issues_skips_open_duplicate(monkeypatch):
    # An open issue already matches the PII summary -> only the health issue is made.
    router = make_router(existing_summaries=["HIGH-risk PII"])
    monkeypatch.setattr(jira, "jira_request", router)
    res = jira.Client(CONFIG).create_issues(EVENTS)
    assert res["skipped"] == 1 and len(res["created"]) == 1


def test_missing_config():
    with pytest.raises(IntegrationError):
        jira.Client({"base_url": "x"}).test()


def test_cli_test(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(jira, "jira_request", make_router())
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"jira": CONFIG}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["jira", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "Security" in res.output
