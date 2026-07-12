"""ServiceNow connector tests — sn_request monkeypatched, no network."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.integrations import servicenow as sn
from sqldoc.integrations.base import FindingEvent, IntegrationError


CONFIG = {"instance_url": "https://acme.service-now.com/", "username": "svc",
          "password": "pw", "ci_class": "cmdb_ci_database"}


def make_router(ci_exists=True):
    calls = []
    counter = {"n": 0}

    def router(method, path, cfg, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/now/table/incident" and method == "GET":
            return {"result": []}
        if path == "/api/now/table/incident" and method == "POST":
            counter["n"] += 1
            return {"result": {"number": f"INC{counter['n']:04d}", "sys_id": "abc"}}
        if path == "/api/now/table/change_request" and method == "POST":
            return {"result": {"number": "CHG0001"}}
        if path.startswith("/api/now/table/cmdb_ci_database") and method == "GET":
            return {"result": ([{"sys_id": "CI1", "name": "DB"}] if ci_exists else [])}
        if "/cmdb_ci_database/" in path and method == "PATCH":
            return {"result": {"sys_id": "CI1"}}
        return {}

    router.calls = calls
    return router


EVENTS = [
    FindingEvent("security", "high", "[sqldoc] Security score 40/100 below threshold on DB",
                 "weak config", database="DB"),
    FindingEvent("pii", "high", "[sqldoc] 3 HIGH-risk PII column(s) in DB", "SSN", database="DB"),
]
METRICS = {"database": "DB", "tables": 20, "pii_findings": 3, "security_score": 40,
           "health_score": 70}


def test_test_ok(monkeypatch):
    monkeypatch.setattr(sn, "sn_request", make_router())
    res = sn.Client(CONFIG).test()
    assert res["ok"] and "service-now" in res["detail"]


def test_create_incidents_sets_urgency(monkeypatch):
    router = make_router()
    monkeypatch.setattr(sn, "sn_request", router)
    res = sn.Client(CONFIG).create_issues(EVENTS, metrics=METRICS)
    assert res["ok"] and len(res["created"]) == 2
    posts = [c for c in router.calls if c[1] == "/api/now/table/incident" and c[0] == "POST"]
    # high severity -> urgency 2, impact 1
    assert posts[0][2]["json"]["urgency"] == 2
    assert res["ci_updated"] is True


def test_ci_update_when_no_incidents(monkeypatch):
    router = make_router()
    monkeypatch.setattr(sn, "sn_request", router)
    # No events, but the CI record should still be refreshed on push.
    res = sn.Client(CONFIG).create_issues([], metrics=METRICS)
    assert res["created"] == [] and res["ci_updated"] is True
    patches = [c for c in router.calls if c[0] == "PATCH"]
    assert len(patches) == 1


def test_ci_missing_is_non_fatal(monkeypatch):
    monkeypatch.setattr(sn, "sn_request", make_router(ci_exists=False))
    res = sn.Client(CONFIG).create_issues(EVENTS, metrics=METRICS)
    assert res["ci_updated"] is False and len(res["created"]) == 2


def test_change_request(monkeypatch):
    router = make_router()
    monkeypatch.setattr(sn, "sn_request", router)
    num = sn.Client(CONFIG).create_change_request("PROD", "table Orders added a column")
    assert num == "CHG0001"


def test_missing_config():
    with pytest.raises(IntegrationError):
        sn.Client({"instance_url": "x"}).test()


def test_cli_test(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(sn, "sn_request", make_router())
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"servicenow": CONFIG}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["servicenow", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "ServiceNow" in res.output
