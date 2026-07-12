"""Cover remaining connector transports + error/missing-config branches."""
import sys
import types

import pytest
import requests

from sqldoc.integrations.base import IntegrationError


class FakeResp:
    def __init__(self, status=200, json_data=None, text="", content=b"{}", headers=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = content
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        return self._json


@pytest.fixture
def http(monkeypatch):
    state = {"resp": FakeResp()}
    monkeypatch.setattr(requests, "request", lambda *a, **k: state["resp"])
    monkeypatch.setattr(requests, "post", lambda *a, **k: state["resp"])
    monkeypatch.setattr(requests, "get", lambda *a, **k: state["resp"])
    return state


# --- servicenow transport --------------------------------------------------

def test_servicenow_sn_request(http):
    from sqldoc.integrations.servicenow import sn_request, _base
    http["resp"] = FakeResp(201, {"result": {"number": "INC1"}})
    assert sn_request("POST", "/api/now/table/incident", {"instance_url": "https://x",
                      "username": "u", "password": "p"})["result"]["number"] == "INC1"
    http["resp"] = FakeResp(400, text="bad")
    with pytest.raises(IntegrationError):
        sn_request("GET", "/x", {"instance_url": "https://x", "username": "u", "password": "p"})


def test_servicenow_base_missing():
    from sqldoc.integrations.servicenow import _base
    with pytest.raises(IntegrationError):
        _base({})


# --- confluence transport --------------------------------------------------

def test_confluence_api_request(http):
    from sqldoc.integrations.confluence import api_request, _base
    http["resp"] = FakeResp(200, {"results": []})
    assert api_request("GET", "https://acme/wiki/api/v2/spaces",
                       {"email": "e", "api_token": "t"}) == {"results": []}
    http["resp"] = FakeResp(500, text="err")
    with pytest.raises(IntegrationError):
        api_request("GET", "https://acme/x", {"email": "e", "api_token": "t"})


def test_confluence_base_required():
    from sqldoc.integrations.confluence import _base
    with pytest.raises(IntegrationError):
        _base({})


# --- azuredevops transport -------------------------------------------------

def test_ado_request(http):
    from sqldoc.integrations.azuredevops import ado_request, _org_url
    http["resp"] = FakeResp(200, {"value": []})
    assert ado_request("GET", "https://dev.azure.com/x", {"pat": "t"}) == {"value": []}
    http["resp"] = FakeResp(403, text="no")
    with pytest.raises(IntegrationError):
        ado_request("GET", "https://dev.azure.com/x", {"pat": "t"})


def test_ado_org_url_missing():
    from sqldoc.integrations.azuredevops import _org_url
    with pytest.raises(IntegrationError):
        _org_url({})


# --- jira transport --------------------------------------------------------

def test_jira_request(http):
    from sqldoc.integrations.jira import jira_request
    http["resp"] = FakeResp(200, {"key": "SEC-1"})
    cfg = {"base_url": "https://acme.atlassian.net", "email": "e", "api_token": "t"}
    assert jira_request("GET", "/rest/api/3/myself", cfg)["key"] == "SEC-1"
    http["resp"] = FakeResp(404, text="nope")
    with pytest.raises(IntegrationError):
        jira_request("GET", "/x", cfg)


# --- notion transport ------------------------------------------------------

def test_notion_request(http):
    from sqldoc.integrations.notion import notion_request
    http["resp"] = FakeResp(200, {"id": "p"})
    assert notion_request("GET", "/users/me", {"token": "t"})["id"] == "p"
    http["resp"] = FakeResp(401, text="unauth")
    with pytest.raises(IntegrationError):
        notion_request("GET", "/x", {"token": "t"})


# --- gdrive build_service (fake google) ------------------------------------

def test_gdrive_build_service(monkeypatch):
    from sqldoc.integrations import gdrive

    class Creds:
        pass
    sa = types.SimpleNamespace(Credentials=types.SimpleNamespace(
        from_service_account_file=lambda f, scopes=None: Creds(),
        from_service_account_info=lambda i, scopes=None: Creds()))
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery",
                        types.SimpleNamespace(build=lambda *a, **k: "DRIVE"))
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", types.ModuleType("google.oauth2"))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", sa)
    assert gdrive.build_service({"service_account_file": "/x.json"}) == "DRIVE"


def test_gdrive_build_service_needs_creds(monkeypatch):
    from sqldoc.integrations import gdrive
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery",
                        types.SimpleNamespace(build=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", types.ModuleType("google.oauth2"))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account",
                        types.SimpleNamespace(Credentials=object()))
    with pytest.raises(IntegrationError):
        gdrive.build_service({})


# --- github_wiki run_git (fake subprocess) ---------------------------------

def test_github_run_git(monkeypatch):
    from sqldoc.integrations import github_wiki
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    assert github_wiki.run_git(["ls-remote", "url"]) == "ok"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="fatal: no"))
    with pytest.raises(IntegrationError):
        github_wiki.run_git(["clone", "url"])


# --- sarif -----------------------------------------------------------------

def test_sarif_build_and_render(tmp_path):
    import json
    from sqldoc.sarif import build_sarif, render_sarif
    from sqldoc.pii import Finding
    findings = [Finding("Sales", "Customer", "SSN", "varchar", "government_id",
                        "HIGH", "name", ["HIPAA"], "Encrypt")]
    doc = build_sarif("DB", findings)
    assert doc["version"] == "2.1.0" and doc["runs"][0]["results"]
    assert build_sarif("DB", [])["runs"][0]["results"] == []
    out = tmp_path / "r.sarif"
    render_sarif("DB", findings, str(out))
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == "2.1.0"
