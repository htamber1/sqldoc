"""Escalation routes to admin, and TAKE OWNERSHIP as a flag rather than a level.

Extends the implicit-admin detection beyond sysadmin / securityadmin /
CONTROL SERVER / CONTROL:

  * ALTER ANY LOGIN  -- its holder can reset any SQL login's password (including
                        sa) and so can reach sysadmin at will.
  * IMPERSONATE a PRIVILEGED principal, at server scope (class 101) or database
                        scope (class 4). Impersonating an ORDINARY principal is
                        not an escalation and must not read as admin, so both
                        queries resolve the grant's target.
  * TAKE OWNERSHIP   -- a route, not access: the holder can take control but has
                        not been given it. Recorded on the new `flags` field so a
                        reviewer sees the risk without the effective level being
                        overstated.

No database required.
"""
import pytest

from sqldoc.access.model import Login
from sqldoc.access.sqlserver import (ESCALATION_FLAG_PERMISSIONS, IMPERSONATE_PREFIX,
                                     SERVER_PERMISSION_LEVEL, _server_grantors,
                                     _server_implied_level, collect_db_access,
                                     collect_server_logins)


class Row(dict):
    pass


class FakeCursor:
    """Dispatches on each query's /* MARKER */ comment."""

    def __init__(self, **sets):
        self._by_marker = {
            "ACCESS_SERVER_LOGINS": list(sets.get("logins", ())),
            "ACCESS_SERVER_ROLE_MEMBERS": list(sets.get("server_roles", ())),
            "ACCESS_SERVER_PERMISSIONS": list(sets.get("server_perms", ())),
            "ACCESS_DB_PRINCIPALS": list(sets.get("principals", ())),
            "ACCESS_DB_ROLE_MEMBERS": list(sets.get("role_members", ())),
            "ACCESS_DB_PERMISSIONS": list(sets.get("perms", ())),
            "ACCESS_DB_SCOPED_PERMISSIONS": list(sets.get("scoped", ())),
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
    def __init__(self, schema, table):
        self.schema, self.table = schema, table
        self.risk, self.regulations = "HIGH", ["GDPR"]


PII = [Finding("dbo", "Customers"), Finding("dbo", "Payments")]
GRP = "DOM\\D-Group"
ADMINS = "DOM\\D-Admins"
PRINCIPALS = [Row(db_user=GRP, type_desc="WINDOWS_GROUP")]


def srv_perm(principal, permission, state="GRANT", perm_class=100, target=None):
    return Row(principal_name=principal, permission_name=permission,
               state_desc=state, perm_class=perm_class, target_name=target)


def db_perm(principal, permission, state="GRANT", perm_class=0, target=None):
    return Row(principal_name=principal, permission_name=permission,
               state_desc=state, perm_class=perm_class, target_name=target)


def logins_from(**sets):
    sets.setdefault("logins", [Row(name=GRP, type_desc="WINDOWS_GROUP", is_disabled=0),
                               Row(name=ADMINS, type_desc="WINDOWS_GROUP", is_disabled=0)])
    return {lg.name: lg for lg in collect_server_logins(FakeCursor(**sets))}


def db_access(**sets):
    return collect_db_access(FakeCursor(**sets), "srv", "AppDB",
                             [Login(name=GRP, type="WINDOWS_GROUP")], PII)


# --- ALTER ANY LOGIN --------------------------------------------------------

def test_alter_any_login_maps_to_admin():
    assert SERVER_PERMISSION_LEVEL.get("ALTER ANY LOGIN") == "admin"


def test_alter_any_login_is_detected():
    login = Login(name=GRP, server_permissions=["ALTER ANY LOGIN"])
    assert _server_implied_level(login) == "admin"


def test_alter_any_login_yields_an_admin_row_naming_it():
    rows = collect_db_access(
        FakeCursor(), "srv", "AppDB",
        [Login(name=GRP, type="WINDOWS_GROUP",
               server_permissions=["ALTER ANY LOGIN"])], PII)
    assert rows[0].level == "admin"
    assert "ALTER ANY LOGIN" in rows[0].via


# --- IMPERSONATE at server scope --------------------------------------------

def test_impersonating_a_sysadmin_login_is_recorded_and_levelled():
    logins = logins_from(
        server_roles=[Row(role_name="sysadmin", member_name=ADMINS)],
        server_perms=[srv_perm(GRP, "IMPERSONATE", perm_class=101, target=ADMINS)])
    assert logins[GRP].server_permissions == [f"{IMPERSONATE_PREFIX}LOGIN::{ADMINS}"]
    assert _server_implied_level(logins[GRP]) == "admin"
    assert _server_grantors(logins[GRP]) == logins[GRP].server_permissions


def test_impersonating_an_ordinary_login_confers_nothing():
    """The whole point of resolving the target: impersonating a non-privileged
    principal is not an escalation and must not read as admin."""
    logins = logins_from(server_perms=[
        srv_perm(GRP, "IMPERSONATE", perm_class=101, target="DOM\\D-Ordinary")])
    assert logins[GRP].server_permissions == []
    assert _server_implied_level(logins[GRP]) == "none"


def test_impersonating_sa_is_always_an_escalation():
    logins = logins_from(server_perms=[
        srv_perm(GRP, "IMPERSONATE", perm_class=101, target="sa")])
    assert _server_implied_level(logins[GRP]) == "admin"


def test_a_control_server_holder_counts_as_a_privileged_target():
    logins = logins_from(server_perms=[
        srv_perm(ADMINS, "CONTROL SERVER"),
        srv_perm(GRP, "IMPERSONATE", perm_class=101, target=ADMINS)])
    assert _server_implied_level(logins[GRP]) == "admin"


def test_a_denied_impersonate_confers_nothing():
    logins = logins_from(
        server_roles=[Row(role_name="sysadmin", member_name=ADMINS)],
        server_perms=[srv_perm(GRP, "IMPERSONATE", state="DENY",
                               perm_class=101, target=ADMINS)])
    assert _server_implied_level(logins[GRP]) == "none"


# --- IMPERSONATE at database scope ------------------------------------------

def test_impersonating_dbo_is_admin():
    rows = db_access(principals=PRINCIPALS,
                     scoped=[db_perm(GRP, "IMPERSONATE", perm_class=4, target="dbo")])
    assert rows[0].level == "admin"
    assert len(rows[0].pii_tables) == 2


def test_impersonating_a_db_owner_member_is_admin():
    rows = db_access(
        principals=PRINCIPALS,
        role_members=[Row(member_name="AppOwner", role_name="db_owner")],
        scoped=[db_perm(GRP, "IMPERSONATE", perm_class=4, target="AppOwner")])
    assert rows[0].level == "admin"


def test_impersonating_an_ordinary_db_user_confers_nothing():
    """The group has a database principal, so a row still exists (pre-existing
    behaviour for a db user with no effective grants) -- it must simply not be
    elevated, and must expose no PII."""
    rows = db_access(
        principals=PRINCIPALS,
        scoped=[db_perm(GRP, "IMPERSONATE", perm_class=4, target="ReportReader")])
    assert rows[0].level == "none"
    assert rows[0].pii_tables == []


# --- TAKE OWNERSHIP: flagged, not levelled ----------------------------------

def test_take_ownership_is_declared_as_a_flag_not_a_level():
    assert "TAKE OWNERSHIP" in ESCALATION_FLAG_PERMISSIONS
    assert "TAKE OWNERSHIP" not in SERVER_PERMISSION_LEVEL


@pytest.fixture
def take_ownership_rows():
    return db_access(
        principals=PRINCIPALS,
        role_members=[Row(member_name=GRP, role_name="db_datareader")],
        scoped=[db_perm(GRP, "TAKE OWNERSHIP")])


def test_take_ownership_does_not_inflate_the_level(take_ownership_rows):
    assert take_ownership_rows[0].level == "read"


def test_take_ownership_is_flagged_with_an_explanation(take_ownership_rows):
    flags = take_ownership_rows[0].flags
    assert any("TAKE OWNERSHIP" in f for f in flags)
    assert any("escalation route" in f for f in flags)


def test_a_denied_take_ownership_is_not_flagged():
    rows = db_access(
        principals=PRINCIPALS,
        role_members=[Row(member_name=GRP, role_name="db_datareader")],
        scoped=[db_perm(GRP, "TAKE OWNERSHIP", state="DENY")])
    assert rows[0].flags == []


# --- no regressions on the earlier rules ------------------------------------

def test_sysadmin_is_still_admin():
    rows = collect_db_access(
        FakeCursor(), "srv", "AppDB",
        [Login(name=GRP, type="WINDOWS_GROUP", server_roles=["sysadmin"])], PII)
    assert rows[0].level == "admin"


def test_database_control_is_still_admin():
    rows = db_access(principals=PRINCIPALS, scoped=[db_perm(GRP, "CONTROL")])
    assert rows[0].level == "admin"


def test_ordinary_permissions_still_confer_nothing():
    login = Login(name=GRP, server_permissions=["VIEW ANY DEFINITION", "CONNECT SQL"])
    assert _server_implied_level(login) == "none"


def test_a_plain_reader_has_no_flags():
    rows = db_access(principals=PRINCIPALS,
                     role_members=[Row(member_name=GRP, role_name="db_datareader")])
    assert rows[0].flags == []
