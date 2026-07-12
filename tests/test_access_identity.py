"""Identity-provider expansion: Okta, Google Workspace, JumpCloud, generic LDAP,
hybrid AD, native SQL — all transports mocked."""
import pytest

from sqldoc.access import ad
from sqldoc.access.ad import (
    get_source, LdapADSource, GraphADSource, OktaADSource, GoogleWorkspaceADSource,
    JumpCloudADSource, NativeSource, _auto_detect)
from sqldoc.integrations.base import IntegrationError


# --- source selection / auto-detect ----------------------------------------

@pytest.mark.parametrize("cfg,expected_cls", [
    ({"type": "okta", "okta_domain": "https://x.okta.com"}, OktaADSource),
    ({"type": "google"}, GoogleWorkspaceADSource),
    ({"type": "jumpcloud"}, JumpCloudADSource),
    ({"type": "native"}, NativeSource),
    ({"type": "generic-ldap", "server": "ldap://x", "base_dn": "dc=x"}, LdapADSource),
    ({"type": "hybrid", "tenant_id": "t", "client_id": "c", "client_secret": "s"}, GraphADSource),
])
def test_get_source_explicit(cfg, expected_cls):
    assert isinstance(get_source(cfg), expected_cls)


@pytest.mark.parametrize("cfg,kind", [
    ({"okta_domain": "https://x.okta.com"}, "okta"),
    ({"delegated_admin": "admin@x.com"}, "google"),
    ({"jumpcloud_api_key": "k"}, "jumpcloud"),
    ({"tenant_id": "t"}, "graph"),
    ({"server": "ldap://x", "base_dn": "dc=x"}, "ldap"),
    ({}, "native"),
])
def test_auto_detect(cfg, kind):
    assert _auto_detect(cfg) == kind


def test_hybrid_flag_sets_source():
    src = get_source({"type": "hybrid", "tenant_id": "t", "client_id": "c", "client_secret": "s"})
    assert src.hybrid and src.source == "hybrid"


def test_unknown_type():
    with pytest.raises(IntegrationError):
        get_source({"type": "banana"})


# --- native ----------------------------------------------------------------

def test_native_source():
    u = NativeSource().get_user("sql_login")
    assert u.found and u.login == "sql_login" and u.groups == [] and u.source == "native"


# --- generic LDAP ----------------------------------------------------------

class _FakeLdapConn:
    def __init__(self, entries):
        self._entries, self.entries = entries, []

    def search(self, base, flt, search_scope=None, attributes=None):
        self.flt = flt
        self.entries = self._entries


def test_generic_ldap_uses_uid(monkeypatch):
    entry = {"uid": "jdoe", "mail": "jdoe@corp.org", "cn": "John Doe",
             "memberOf": ["cn=readers,ou=groups,dc=corp,dc=org"], "title": "Analyst",
             "departmentNumber": "Sales"}
    conn = _FakeLdapConn([entry])
    monkeypatch.setattr(ad, "build_connection", lambda cfg: conn)
    src = LdapADSource({"server": "ldap://x", "base_dn": "dc=corp,dc=org"}, generic=True)
    u = src.get_user("jdoe")
    assert u.found and u.sam_account_name == "jdoe" and u.display_name == "John Doe"
    assert u.groups == ["readers"] and u.department == "Sales"
    assert "uid=jdoe" in conn.flt          # searched the generic attribute


def test_ldap_attribute_override(monkeypatch):
    entry = {"sAMAccountName": "x", "mail": "x@y", "customGroups": ["cn=g1,dc=y"]}
    conn = _FakeLdapConn([entry])
    monkeypatch.setattr(ad, "build_connection", lambda cfg: conn)
    src = LdapADSource({"server": "ldap://x", "base_dn": "dc=y",
                        "attributes": {"member": "customGroups"}})
    u = src.get_user("x")
    assert u.groups == ["g1"]


# --- hybrid graph ----------------------------------------------------------

def test_hybrid_prefers_onprem_login(monkeypatch):
    monkeypatch.setattr(ad, "acquire_token", lambda cfg: "T")

    def fake_get(path, token, params=None):
        if path == "/users/jsmith@corp.com":
            return {"displayName": "Jane", "userPrincipalName": "jsmith@corp.com",
                    "onPremisesSamAccountName": "jsmith", "onPremisesDomainName": "corp.local",
                    "jobTitle": "Analyst", "department": "Sales", "accountEnabled": True}
        return {"value": [{"displayName": "Sales Read"}]}
    monkeypatch.setattr(ad, "graph_get", fake_get)
    src = GraphADSource({"tenant_id": "t", "client_id": "c", "client_secret": "s"}, hybrid=True)
    u = src.get_user("jsmith@corp.com")
    assert u.login == "CORP\\jsmith" and u.source == "hybrid"


# --- Okta ------------------------------------------------------------------

def test_okta_get_user(monkeypatch):
    def fake(method, path, cfg, **k):
        if path == "/api/v1/users/jsmith@corp.com":
            return {"id": "00u1", "status": "ACTIVE", "profile": {
                "firstName": "Jane", "lastName": "Smith", "email": "jsmith@corp.com",
                "login": "jsmith@corp.com", "title": "Analyst", "department": "Sales"}}
        if path == "/api/v1/users/00u1/groups":
            return [{"profile": {"name": "Sales Read"}}, {"profile": {"name": "Everyone"}}]
        return None
    monkeypatch.setattr(ad, "okta_request", fake)
    u = OktaADSource({"okta_domain": "https://acme.okta.com", "api_token": "t"}).get_user("jsmith@corp.com")
    assert u.found and u.display_name == "Jane Smith" and u.department == "Sales"
    assert u.groups == ["Sales Read", "Everyone"] and u.source == "okta"


def test_okta_not_found(monkeypatch):
    monkeypatch.setattr(ad, "okta_request", lambda *a, **k: None)
    u = OktaADSource({"okta_domain": "https://x.okta.com", "api_token": "t"}).get_user("ghost")
    assert not u.found


def test_okta_needs_token():
    with pytest.raises(IntegrationError):
        OktaADSource({"okta_domain": "https://x.okta.com"}).get_user("x")


# --- Google Workspace ------------------------------------------------------

class _GExec:
    def __init__(self, v):
        self._v = v

    def execute(self):
        return self._v


class _GUsers:
    def get(self, userKey=None):
        return _GExec({"primaryEmail": "jsmith@corp.com", "name": {"fullName": "Jane Smith"},
                       "organizations": [{"title": "Analyst", "department": "Sales"}],
                       "suspended": False})


class _GGroups:
    def list(self, userKey=None, maxResults=None):
        return _GExec({"groups": [{"name": "Sales Read", "email": "sales@corp.com"}]})


class _GService:
    def users(self):
        return _GUsers()

    def groups(self):
        return _GGroups()


def test_google_get_user(monkeypatch):
    monkeypatch.setattr(ad, "build_directory_service", lambda cfg: _GService())
    u = GoogleWorkspaceADSource({"service_account_file": "x",
                                 "delegated_admin": "admin@corp.com"}).get_user("jsmith@corp.com")
    assert u.found and u.display_name == "Jane Smith" and u.title == "Analyst"
    assert u.groups == ["Sales Read"] and u.source == "google"


# --- JumpCloud -------------------------------------------------------------

def test_jumpcloud_get_user(monkeypatch):
    def fake(method, path, cfg, **k):
        if path == "/api/search/systemusers":
            return {"results": [{"_id": "u1", "username": "jsmith", "email": "jsmith@corp.com",
                                 "displayname": "Jane Smith", "jobTitle": "Analyst",
                                 "department": "Sales", "suspended": False}]}
        if path == "/api/v2/users/u1/memberof":
            return [{"id": "g1"}, {"id": "g2"}]
        if path == "/api/v2/usergroups":
            return [{"id": "g1", "name": "Sales Read"}, {"id": "g2", "name": "Staff"}]
        return None
    monkeypatch.setattr(ad, "jc_request", fake)
    u = JumpCloudADSource({"api_key": "k"}).get_user("jsmith@corp.com")
    assert u.found and u.display_name == "Jane Smith"
    assert set(u.groups) == {"Sales Read", "Staff"} and u.source == "jumpcloud"


def test_jumpcloud_not_found(monkeypatch):
    monkeypatch.setattr(ad, "jc_request", lambda *a, **k: {"results": []})
    u = JumpCloudADSource({"api_key": "k"}).get_user("ghost")
    assert not u.found
