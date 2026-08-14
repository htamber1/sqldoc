"""The AD access chain: passwordless bind, nested groups, implicit sysadmin.

Regression cover for three defects found running the chain against a real
directory and SQL Server estate, all of which produced the SAME failure mode --
reporting "no access" for a principal that actually held full control:

  1. No integrated auth -- the LDAP bind did simple/anonymous only, forcing a
     bind password to be stored in plaintext.
  2. Nested groups invisible -- only the direct `memberOf` attribute was read,
     so a user in G-Role nested into D-Resource (the group that is the actual
     SQL login) looked unprivileged. Measured live: 48 groups seen vs 213 actual.
  3. sysadmin reported as no access -- a matched login with no database
     principal was skipped, but sysadmin HAS none: it bypasses permission
     checks entirely and maps to dbo in every database.

No directory or database required; a stand-in ldap3 and a fake cursor drive it.
"""
import types

import pytest

from sqldoc.access import ad as ad_mod
from sqldoc.access.config import ad_config
from sqldoc.access.model import Login
from sqldoc.access.sqlserver import (SERVER_ROLE_LEVEL, _server_implied_level,
                                     collect_db_access)


# --- fakes ------------------------------------------------------------------

class FakeConn:
    def __init__(self, **kw):
        self.kw = kw


def fake_ldap3(record, fail_sasl=False):
    """A stand-in ldap3 module that records how Connection() was called."""
    module = types.SimpleNamespace(ALL="ALL", SASL="SASL", KERBEROS="GSSAPI")
    module.Server = lambda *a, **kw: ("server", a, kw)

    def Connection(server, **kw):
        if fail_sasl and kw.get("authentication") == "SASL":
            raise _MissingBackend("package gssapi (or winkerberos) missing")
        record.update(kw)
        return FakeConn(**kw)

    module.Connection = Connection
    return module


class _MissingBackend(Exception):
    pass


# ldap3 raises this class by name when no GSSAPI backend is importable.
_MissingBackend.__name__ = "LDAPPackageUnavailableError"


def build_with_fake_ldap3(monkeypatch, cfg, module):
    monkeypatch.setattr(ad_mod, "require", lambda *a, **kw: module)
    return ad_mod.build_connection(cfg)


class FakeEntry:
    def __init__(self, cn):
        self.cn = cn


class FakeSearchConn:
    def __init__(self, entries):
        self._entries = entries
        self.filters = []

    def search(self, base, flt, **kw):
        self.filters.append(flt)
        self.entries = self._entries


class Row(dict):
    pass


class FakeCursor:
    """Dispatches on each query's /* MARKER */ comment, so the test does not
    depend on the order collect_db_access happens to run its queries in."""

    def __init__(self, principals=(), role_members=(), perms=(), scoped=()):
        self._by_marker = {
            "ACCESS_DB_PRINCIPALS": list(principals),
            "ACCESS_DB_ROLE_MEMBERS": list(role_members),
            "ACCESS_DB_PERMISSIONS": list(perms),
            "ACCESS_DB_SCOPED_PERMISSIONS": list(scoped),
        }
        self._rows = []

    def execute(self, sql, *a):
        self._rows = []
        for marker, rows in self._by_marker.items():
            if marker in sql:
                self._rows = rows
                break

    def fetchall(self):
        return self._rows


class Finding:
    def __init__(self, schema, table, risk="HIGH", regulations=("GDPR",)):
        self.schema, self.table = schema, table
        self.risk, self.regulations = risk, list(regulations)


PII = [Finding("dbo", "Customers"), Finding("dbo", "Payments")]


# --- 1. passwordless (integrated) bind --------------------------------------

def test_windows_auth_binds_over_sasl_kerberos(monkeypatch):
    rec = {}
    build_with_fake_ldap3(monkeypatch, {"server": "dc.test", "windows_auth": True},
                          fake_ldap3(rec))
    assert rec.get("authentication") == "SASL"
    assert rec.get("sasl_mechanism") == "GSSAPI"


def test_windows_auth_sends_no_credentials(monkeypatch):
    rec = {}
    build_with_fake_ldap3(monkeypatch, {"server": "dc.test", "windows_auth": True},
                          fake_ldap3(rec))
    assert rec.get("user") is None
    assert rec.get("password") is None


def test_simple_bind_is_unchanged(monkeypatch):
    rec = {}
    build_with_fake_ldap3(monkeypatch,
                          {"server": "dc.test", "bind_dn": "CN=svc",
                           "bind_password": "pw"}, fake_ldap3(rec))
    assert rec.get("user") == "CN=svc"
    assert rec.get("password") == "pw"
    assert "authentication" not in rec


def test_missing_kerberos_backend_names_the_install(monkeypatch):
    """ldap3's LDAPPackageUnavailableError is internal; the user needs the pip
    install that fixes it."""
    with pytest.raises(Exception) as excinfo:
        build_with_fake_ldap3(monkeypatch,
                              {"server": "dc.test", "windows_auth": True},
                              fake_ldap3({}, fail_sasl=True))
    assert "winkerberos" in str(excinfo.value)
    assert "gssapi" in str(excinfo.value)


# --- 1b. ad_config inheritance ----------------------------------------------

def test_ad_config_inherits_top_level_windows_auth():
    cfg = {"windows_auth": True, "access": {"ad": {"server": "dc"}}}
    assert ad_config(cfg)["windows_auth"] is True


def test_ad_config_explicit_false_wins_over_top_level_true():
    cfg = {"windows_auth": True,
           "access": {"ad": {"server": "dc", "windows_auth": False}}}
    assert ad_config(cfg)["windows_auth"] is False


def test_ad_config_never_inherits_over_an_explicit_bind_dn():
    """A global windows_auth (meant for SQL Server) must not silently discard a
    directory service account the user configured."""
    cfg = {"windows_auth": True,
           "access": {"ad": {"server": "dc", "bind_dn": "CN=svc"}}}
    assert ad_config(cfg).get("windows_auth") is None


def test_no_ad_section_stays_empty():
    assert ad_config({"windows_auth": True}) == {}


# --- 2. nested (transitive) group expansion ---------------------------------

def test_in_chain_is_ads_matching_rule_oid():
    assert ad_mod.IN_CHAIN == "1.2.840.113556.1.4.1941"


@pytest.mark.parametrize("direct,nested,expected", [
    (["G-Role"], ["g-role", "D-Resource"], ["G-Role", "D-Resource"]),
    (["A", "B"], ["C"], ["A", "B", "C"]),
    (["A", "", None], [], ["A"]),
])
def test_merge_groups_unions_and_dedupes(direct, nested, expected):
    assert ad_mod._merge_groups(direct, nested) == expected


def test_nested_groups_are_returned():
    src = ad_mod.LdapADSource({"server": "dc", "base_dn": "DC=t"})
    conn = FakeSearchConn([FakeEntry("D-Resource"), FakeEntry("D-Other")])
    assert src._nested_groups(conn, "CN=u,DC=t") == ["D-Resource", "D-Other"]


def test_nested_group_query_uses_the_in_chain_rule():
    src = ad_mod.LdapADSource({"server": "dc", "base_dn": "DC=t"})
    conn = FakeSearchConn([FakeEntry("D-Resource")])
    src._nested_groups(conn, "CN=u,DC=t")
    assert f"member:{ad_mod.IN_CHAIN}:=" in conn.filters[0]


def test_nested_groups_can_be_disabled():
    src = ad_mod.LdapADSource({"server": "dc", "base_dn": "DC=t",
                               "nested_groups": False})
    assert src._nested_groups(FakeSearchConn([FakeEntry("X")]), "CN=u") == []


def test_generic_ldap_skips_the_ad_only_rule():
    src = ad_mod.LdapADSource({"server": "dc", "base_dn": "DC=t"}, generic=True)
    assert src._nested_groups(FakeSearchConn([FakeEntry("X")]), "CN=u") == []


def test_no_user_dn_means_no_expansion():
    src = ad_mod.LdapADSource({"server": "dc", "base_dn": "DC=t"})
    assert src._nested_groups(FakeSearchConn([]), "") == []


def test_a_failing_expansion_is_non_fatal():
    """The direct memberOf list must still stand if the directory rejects the
    Microsoft-specific rule."""
    class ExplodingConn:
        def search(self, *a, **kw):
            raise RuntimeError("directory does not support the rule")

    src = ad_mod.LdapADSource({"server": "dc", "base_dn": "DC=t"})
    assert src._nested_groups(ExplodingConn(), "CN=u") == []


# --- 3. sysadmin implies access to every database ---------------------------

def test_sysadmin_maps_to_admin():
    assert SERVER_ROLE_LEVEL.get("sysadmin") == "admin"


@pytest.mark.parametrize("roles,expected", [
    (["sysadmin"], "admin"),
    (["SysAdmin"], "admin"),
    (["dbcreator", "processadmin"], "none"),
    ([], "none"),
])
def test_server_implied_level(roles, expected):
    assert _server_implied_level(Login(name="X", server_roles=roles)) == expected


@pytest.fixture
def sysadmin_rows():
    """The real-world case: a Windows GROUP login holding sysadmin with NO
    database principal of its own."""
    login = Login(name="DOM\\D-Admins", type="WINDOWS_GROUP",
                  server_roles=["sysadmin"])
    return collect_db_access(FakeCursor(), "srv", "AppDB", [login], PII)


def test_sysadmin_with_no_db_user_yields_an_access_row(sysadmin_rows):
    assert len(sysadmin_rows) == 1


def test_sysadmin_is_reported_as_admin(sysadmin_rows):
    assert sysadmin_rows[0].level == "admin"


def test_sysadmin_db_user_is_empty_not_none(sysadmin_rows):
    assert sysadmin_rows[0].db_user == ""


def test_sysadmin_via_names_the_group_and_the_server_role(sysadmin_rows):
    """The resolution chain is what made these bugs diagnosable at all."""
    via = sysadmin_rows[0].via
    assert "D-Admins" in via
    assert "sysadmin" in via


def test_sysadmin_sees_every_pii_table(sysadmin_rows):
    assert len(sysadmin_rows[0].pii_tables) == 2


def test_non_sysadmin_with_no_db_user_is_still_skipped():
    plain = Login(name="DOM\\D-Nothing", type="WINDOWS_GROUP",
                  server_roles=["dbcreator"])
    assert collect_db_access(FakeCursor(), "srv", "AppDB", [plain], PII) == []


# --- no regression on an ordinary database user -----------------------------

@pytest.fixture
def reader_rows():
    principals = [Row(db_user="DOM\\D-Readers", type_desc="WINDOWS_GROUP")]
    members = [Row(member_name="DOM\\D-Readers", role_name="db_datareader")]
    reader = Login(name="DOM\\D-Readers", type="WINDOWS_GROUP", server_roles=[])
    return collect_db_access(FakeCursor(principals, members), "srv", "AppDB",
                             [reader], PII)


def test_plain_db_datareader_still_resolves(reader_rows):
    assert len(reader_rows) == 1
    assert reader_rows[0].level == "read"
    assert reader_rows[0].roles == ["db_datareader"]


def test_ordinary_reader_has_no_server_wide_suffix(reader_rows):
    assert "server-wide" not in reader_rows[0].via
