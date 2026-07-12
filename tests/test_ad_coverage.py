"""Cover the identity-source transports + error branches in access/ad.py."""
import sys
import types

import pytest
import requests

from sqldoc.access import ad
from sqldoc.integrations.base import IntegrationError


class FakeResp:
    def __init__(self, status=200, json_data=None, text="", content=b"{}"):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = content

    def json(self):
        return self._json


@pytest.fixture
def http(monkeypatch):
    state = {"resp": FakeResp()}
    monkeypatch.setattr(requests, "request", lambda *a, **k: state["resp"])
    monkeypatch.setattr(requests, "get", lambda *a, **k: state["resp"])
    monkeypatch.setattr(requests, "post", lambda *a, **k: state["resp"])
    return state


# --- okta transport --------------------------------------------------------

def test_okta_request_success_404_error(http):
    http["resp"] = FakeResp(200, {"id": "1"})
    assert ad.okta_request("GET", "/api/v1/users/x", {"okta_domain": "https://x", "api_token": "t"})["id"] == "1"
    http["resp"] = FakeResp(404)
    assert ad.okta_request("GET", "/api/v1/users/x", {"okta_domain": "https://x", "api_token": "t"}) is None
    http["resp"] = FakeResp(500, text="err")
    with pytest.raises(IntegrationError):
        ad.okta_request("GET", "/x", {"okta_domain": "https://x", "api_token": "t"})


def test_okta_request_needs_domain():
    with pytest.raises(IntegrationError):
        ad.okta_request("GET", "/x", {})


# --- graph transport -------------------------------------------------------

def test_graph_get_success_404_error(http):
    http["resp"] = FakeResp(200, {"displayName": "J"})
    assert ad.graph_get("/users/x", "tok")["displayName"] == "J"
    http["resp"] = FakeResp(404)
    assert ad.graph_get("/users/x", "tok") is None
    http["resp"] = FakeResp(403, text="denied")
    with pytest.raises(IntegrationError):
        ad.graph_get("/users/x", "tok")


# --- jumpcloud transport ---------------------------------------------------

def test_jc_request(http):
    http["resp"] = FakeResp(200, {"results": []})
    assert ad.jc_request("POST", "/api/search/systemusers", {"api_key": "k", "org_id": "o"}) == {"results": []}
    http["resp"] = FakeResp(404)
    assert ad.jc_request("GET", "/x", {"api_key": "k"}) is None
    http["resp"] = FakeResp(500, text="e")
    with pytest.raises(IntegrationError):
        ad.jc_request("GET", "/x", {"api_key": "k"})


# --- msal-backed token (fake msal) -----------------------------------------

def test_acquire_token_and_failure(monkeypatch):
    ok_app = types.SimpleNamespace(acquire_token_for_client=lambda scopes: {"access_token": "T"})
    monkeypatch.setitem(sys.modules, "msal",
                        types.SimpleNamespace(ConfidentialClientApplication=lambda **k: ok_app))
    assert ad.acquire_token({"tenant_id": "t", "client_id": "c", "client_secret": "s"}) == "T"

    bad_app = types.SimpleNamespace(acquire_token_for_client=lambda scopes: {"error": "x", "error_description": "no"})
    monkeypatch.setitem(sys.modules, "msal",
                        types.SimpleNamespace(ConfidentialClientApplication=lambda **k: bad_app))
    with pytest.raises(IntegrationError):
        ad.acquire_token({"tenant_id": "t", "client_id": "c", "client_secret": "s"})


# --- ldap build_connection (fake ldap3) ------------------------------------

def test_build_connection_fake_ldap3(monkeypatch):
    conn = object()
    fake = types.SimpleNamespace(
        Server=lambda *a, **k: object(), ALL="ALL",
        Connection=lambda *a, **k: conn)
    monkeypatch.setitem(sys.modules, "ldap3", fake)
    assert ad.build_connection({"server": "ldap://x", "bind_dn": "cn=a", "bind_password": "p"}) is conn


# --- google directory service (fake google client) -------------------------

def test_build_directory_service_fake(monkeypatch):
    built = {}

    class Creds:
        def with_subject(self, s):
            built["subject"] = s
            return self
    sa = types.SimpleNamespace(Credentials=types.SimpleNamespace(
        from_service_account_file=lambda f, scopes=None: Creds(),
        from_service_account_info=lambda i, scopes=None: Creds()))
    discovery = types.SimpleNamespace(build=lambda *a, **k: "SERVICE")
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", types.ModuleType("google.oauth2"))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", sa)

    svc = ad.build_directory_service({"service_account_file": "/x.json", "delegated_admin": "admin@x"})
    assert svc == "SERVICE" and built["subject"] == "admin@x"


def test_build_directory_service_needs_creds(monkeypatch):
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery",
                        types.SimpleNamespace(build=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", types.ModuleType("google.oauth2"))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account",
                        types.SimpleNamespace(Credentials=object()))
    with pytest.raises(IntegrationError):
        ad.build_directory_service({})     # no service account


# --- google source get_user error/404 -------------------------------------

def test_google_get_user_not_found(monkeypatch):
    class Svc:
        def users(self):
            raise Exception("404 notFound")
    monkeypatch.setattr(ad, "build_directory_service", lambda cfg: Svc())
    u = ad.GoogleWorkspaceADSource({}).get_user("ghost@x")
    assert not u.found


def test_google_get_user_reraises_other(monkeypatch):
    class Svc:
        def users(self):
            raise Exception("500 server error")
    monkeypatch.setattr(ad, "build_directory_service", lambda cfg: Svc())
    with pytest.raises(Exception):
        ad.GoogleWorkspaceADSource({}).get_user("x@y")
