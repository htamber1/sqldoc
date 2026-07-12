"""Exercise the module-level HTTP transports directly (success / non-2xx / 404),
which are mocked away in the connector unit tests. Patches the global `requests`
functions so no network is used."""
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

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture
def cap(monkeypatch):
    """Capture requests.* calls and drive the response per test."""
    state = {"resp": FakeResp(), "calls": []}

    def _record(method, url, **kw):
        state["calls"].append((method, url, kw))
        return state["resp"]

    monkeypatch.setattr(requests, "request",
                        lambda method, url, **kw: _record(method, url, **kw))
    monkeypatch.setattr(requests, "post", lambda url, **kw: _record("POST", url, **kw))
    monkeypatch.setattr(requests, "get", lambda url, **kw: _record("GET", url, **kw))
    return state


# --- gitlab_wiki -----------------------------------------------------------

def test_gitlab_request_success(cap):
    from sqldoc.integrations.gitlab_wiki import gitlab_request
    cap["resp"] = FakeResp(200, {"name": "P"})
    assert gitlab_request("GET", "/projects/1", {"token": "t"})["name"] == "P"


def test_gitlab_request_404_returns_none(cap):
    from sqldoc.integrations.gitlab_wiki import gitlab_request
    cap["resp"] = FakeResp(404)
    assert gitlab_request("GET", "/projects/1/wikis/x", {"token": "t"}) is None


def test_gitlab_request_error(cap):
    from sqldoc.integrations.gitlab_wiki import gitlab_request
    cap["resp"] = FakeResp(500, text="boom")
    with pytest.raises(IntegrationError):
        gitlab_request("GET", "/projects/1", {"token": "t"})


def test_gitlab_test_and_missing(cap):
    from sqldoc.integrations.gitlab_wiki import Client
    cap["resp"] = FakeResp(200, {"name": "DBDocs"})
    assert Client({"project_id": "1", "token": "t"}).test()["ok"]
    cap["resp"] = FakeResp(404)
    with pytest.raises(IntegrationError):
        Client({"project_id": "1", "token": "t"}).test()


# --- azuredevops_wiki ------------------------------------------------------

def test_ado_wiki_request_variants(cap):
    from sqldoc.integrations.azuredevops_wiki import wiki_request
    cap["resp"] = FakeResp(404)
    assert wiki_request("GET", "http://x", {"pat": "t"}) == (404, None, None)
    cap["resp"] = FakeResp(200, {"ok": 1}, headers={"ETag": "e1"})
    status, data, etag = wiki_request("GET", "http://x", {"pat": "t"})
    assert status == 200 and data == {"ok": 1} and etag == "e1"
    cap["resp"] = FakeResp(500, text="err")
    with pytest.raises(IntegrationError):
        wiki_request("GET", "http://x", {"pat": "t"})


def test_ado_wiki_test(cap):
    from sqldoc.integrations.azuredevops_wiki import Client
    cap["resp"] = FakeResp(200, {}, headers={"ETag": "e"})
    assert Client({"organization": "acme", "project": "Data", "pat": "t"}).test()["ok"]
    cap["resp"] = FakeResp(404)
    with pytest.raises(IntegrationError):
        Client({"organization": "acme", "project": "Data", "pat": "t"}).test()


# --- dropbox ---------------------------------------------------------------

def test_dropbox_rpc_and_upload(cap):
    from sqldoc.integrations.dropbox import dropbox_rpc, dropbox_upload
    cap["resp"] = FakeResp(200, {"name": {"display_name": "Acme"}})
    assert dropbox_rpc("users/get_current_account", {"token": "t"}, None)["name"]["display_name"] == "Acme"
    cap["resp"] = FakeResp(200, {"id": "f"})
    assert dropbox_upload({"token": "t"}, "/a/b.html", b"x")["id"] == "f"
    cap["resp"] = FakeResp(409, text="conflict")
    with pytest.raises(IntegrationError):
        dropbox_upload({"token": "t"}, "/a/b.html", b"x")


# --- nuclino ---------------------------------------------------------------

def test_nuclino_request(cap):
    from sqldoc.integrations.nuclino import nuclino_request
    cap["resp"] = FakeResp(200, {"data": []})
    assert nuclino_request("GET", "/workspaces", {"api_key": "k"}) == {"data": []}
    cap["resp"] = FakeResp(401, text="unauth")
    with pytest.raises(IntegrationError):
        nuclino_request("GET", "/workspaces", {"api_key": "k"})


# --- powerbi ---------------------------------------------------------------

def test_powerbi_post_rows_and_api(cap):
    from sqldoc.integrations import powerbi
    cap["resp"] = FakeResp(200, {})
    powerbi.post_rows_url("https://push", [{"a": 1}])       # ok
    cap["resp"] = FakeResp(400, text="bad")
    with pytest.raises(IntegrationError):
        powerbi.post_rows_url("https://push", [])
    cap["resp"] = FakeResp(200, {"name": "ds"})
    assert powerbi.api_request("GET", "https://api", "tok")["name"] == "ds"
    cap["resp"] = FakeResp(403, text="forbidden")
    with pytest.raises(IntegrationError):
        powerbi.api_request("GET", "https://api", "tok")


def test_powerbi_acquire_token_with_fake_msal(monkeypatch):
    from sqldoc.integrations import powerbi
    fake_app = types.SimpleNamespace(
        acquire_token_for_client=lambda scopes: {"access_token": "TOK"})
    fake_msal = types.SimpleNamespace(
        ConfidentialClientApplication=lambda **kw: fake_app)
    monkeypatch.setitem(sys.modules, "msal", fake_msal)
    tok = powerbi.acquire_token({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
    assert tok == "TOK"


def test_powerbi_acquire_token_failure(monkeypatch):
    from sqldoc.integrations import powerbi
    fake_app = types.SimpleNamespace(
        acquire_token_for_client=lambda scopes: {"error": "invalid_client",
                                                 "error_description": "bad secret"})
    fake_msal = types.SimpleNamespace(ConfidentialClientApplication=lambda **kw: fake_app)
    monkeypatch.setitem(sys.modules, "msal", fake_msal)
    with pytest.raises(IntegrationError):
        powerbi.acquire_token({"tenant_id": "t", "client_id": "c", "client_secret": "s"})


# --- sharepoint token / graph ----------------------------------------------

def test_sharepoint_graph_request(cap):
    from sqldoc.integrations import sharepoint
    cap["resp"] = FakeResp(200, {"displayName": "Site"})
    assert sharepoint.graph_request("GET", "https://g/sites/x", "tok")["displayName"] == "Site"
    cap["resp"] = FakeResp(500, text="err")
    with pytest.raises(IntegrationError):
        sharepoint.graph_request("GET", "https://g/sites/x", "tok")
    # non-JSON 2xx returns {}
    cap["resp"] = FakeResp(204, content=b"", headers={"Content-Type": "text/plain"})
    assert sharepoint.graph_request("PUT", "https://g/x", "tok") == {}
