"""AD source tests — ldap3 / MSAL / network all mocked."""
import pytest

from sqldoc.access import ad
from sqldoc.access.ad import get_source, LdapADSource, GraphADSource
from sqldoc.integrations.base import IntegrationError


# --- source selection ------------------------------------------------------

def test_auto_selects_ldap_for_onprem():
    src = get_source({"type": "auto", "server": "ldap://dc", "base_dn": "DC=corp,DC=local"})
    assert isinstance(src, LdapADSource)


def test_auto_selects_graph_for_cloud():
    src = get_source({"type": "auto", "tenant_id": "t", "client_id": "c", "client_secret": "s"})
    assert isinstance(src, GraphADSource)


def test_explicit_type():
    assert isinstance(get_source({"type": "graph"}), GraphADSource)
    assert isinstance(get_source({"type": "ldap"}), LdapADSource)


def test_unknown_type():
    with pytest.raises(IntegrationError):
        get_source({"type": "banana"})


# --- LDAP ------------------------------------------------------------------

class _FakeLdapConn:
    def __init__(self, entries):
        self._entries = entries
        self.entries = []

    def search(self, base, flt, search_scope=None, attributes=None):
        self.searched = (base, flt)
        self.entries = self._entries


def test_ldap_get_user(monkeypatch):
    entry = {
        "displayName": "Jane Smith", "sAMAccountName": "jsmith",
        "userPrincipalName": "jsmith@corp.com", "mail": "jane@corp.com",
        "distinguishedName": "CN=Jane Smith,OU=Users,DC=corp,DC=local",
        "memberOf": ["CN=Sales Read,OU=Groups,DC=corp,DC=local",
                     "CN=All Staff,OU=Groups,DC=corp,DC=local"],
        "title": "Analyst", "department": "Sales", "userAccountControl": 512,
    }
    monkeypatch.setattr(ad, "build_connection", lambda cfg: _FakeLdapConn([entry]))
    src = LdapADSource({"server": "ldap://dc", "base_dn": "DC=corp,DC=local",
                        "netbios_domain": "CORP"})
    u = src.get_user("jsmith")
    assert u.found and u.display_name == "Jane Smith"
    assert u.sam_account_name == "jsmith" and u.login == "CORP\\jsmith"
    assert u.groups == ["Sales Read", "All Staff"]
    assert u.title == "Analyst" and u.department == "Sales" and u.enabled


def test_ldap_disabled_account(monkeypatch):
    entry = {"sAMAccountName": "svc", "userAccountControl": 514, "memberOf": []}  # 0x2 = disabled
    monkeypatch.setattr(ad, "build_connection", lambda cfg: _FakeLdapConn([entry]))
    u = LdapADSource({"server": "ldap://dc", "base_dn": "DC=corp"}).get_user("svc")
    assert u.found and u.enabled is False


def test_ldap_not_found(monkeypatch):
    monkeypatch.setattr(ad, "build_connection", lambda cfg: _FakeLdapConn([]))
    u = LdapADSource({"server": "ldap://dc", "base_dn": "DC=corp"}).get_user("ghost")
    assert not u.found


def test_ldap_missing_config():
    with pytest.raises(IntegrationError):
        LdapADSource({}).get_user("x")


# --- Graph -----------------------------------------------------------------

def test_graph_get_user(monkeypatch):
    monkeypatch.setattr(ad, "acquire_token", lambda cfg: "TOKEN")

    def fake_get(path, token, params=None):
        assert token == "TOKEN"
        if path == "/users/jsmith@corp.com":
            return {"displayName": "Jane Smith", "userPrincipalName": "jsmith@corp.com",
                    "mail": "jane@corp.com", "jobTitle": "Analyst", "department": "Sales",
                    "accountEnabled": True, "onPremisesSamAccountName": "jsmith"}
        if path.endswith("/transitiveMemberOf"):
            return {"value": [{"displayName": "Sales Read"}, {"displayName": "All Staff"}]}
        return None
    monkeypatch.setattr(ad, "graph_get", fake_get)
    src = GraphADSource({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
    u = src.get_user("jsmith@corp.com")
    assert u.found and u.source == "graph"
    assert u.sam_account_name == "jsmith" and u.department == "Sales"
    assert u.groups == ["Sales Read", "All Staff"]


def test_graph_not_found(monkeypatch):
    monkeypatch.setattr(ad, "acquire_token", lambda cfg: "TOKEN")
    monkeypatch.setattr(ad, "graph_get", lambda path, token, params=None: None)
    u = GraphADSource({"tenant_id": "t", "client_id": "c", "client_secret": "s"}).get_user("ghost")
    assert not u.found


def test_graph_missing_config():
    with pytest.raises(IntegrationError):
        GraphADSource({"tenant_id": "t"}).get_user("x")
