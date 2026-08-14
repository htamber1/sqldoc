"""Every route to implicit full access is detected, not just sysadmin.

In a purely group-based shop (AD G-groups nested into SQL D-group logins, with
no individual logins at all) these are the ONLY ways admin access appears. A
blind spot in any of them means a compliance report showing "no access" for a
principal that in fact controls the database:

  * server ROLE  sysadmin       -- bypasses all permission checks
  * server ROLE  securityadmin  -- can grant itself anything
  * server PERM  CONTROL SERVER -- a grant, so absent from sys.server_role_members
  * database PERM CONTROL       -- class 0, so structurally impossible for the
                                   object-level (class 1) query to return

No database required.
"""
import pytest

from sqldoc.access.model import Login
from sqldoc.access.sqlserver import (DB_SCOPED_PERMISSIONS_SQL,
                                     SERVER_PERMISSION_LEVEL, SERVER_PERMISSIONS_SQL,
                                     SERVER_ROLE_LEVEL, _server_grantors,
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


class Exploding:
    """A cursor whose supplementary queries fail for lack of rights."""

    def __init__(self):
        self._rows = []

    def execute(self, sql, *a):
        if "ACCESS_SERVER_PERMISSIONS" in sql or "ACCESS_DB_SCOPED_PERMISSIONS" in sql:
            raise PermissionError("VIEW SERVER STATE denied")
        self._rows = ([Row(name=GROUP, type_desc="WINDOWS_GROUP", is_disabled=0)]
                      if "ACCESS_SERVER_LOGINS" in sql else [])

    def fetchall(self):
        return self._rows


class Finding:
    def __init__(self, schema, table, risk="HIGH", regulations=("GDPR",)):
        self.schema, self.table = schema, table
        self.risk, self.regulations = risk, list(regulations)


PII = [Finding("dbo", "Customers"), Finding("dbo", "Payments")]
GROUP = "DOM\\D-Group"
PRINCIPALS = [Row(db_user=GROUP, type_desc="WINDOWS_GROUP")]


def access_for(login, **cursor_sets):
    return collect_db_access(FakeCursor(**cursor_sets), "srv", "AppDB", [login], PII)


def group_login(**kw):
    return Login(name=GROUP, type="WINDOWS_GROUP", **kw)


# --- the privilege map ------------------------------------------------------

def test_sysadmin_maps_to_admin():
    assert SERVER_ROLE_LEVEL.get("sysadmin") == "admin"


def test_securityadmin_maps_to_admin():
    assert SERVER_ROLE_LEVEL.get("securityadmin") == "admin"


def test_control_server_maps_to_admin():
    assert SERVER_PERMISSION_LEVEL.get("CONTROL SERVER") == "admin"


def test_ordinary_server_roles_imply_nothing():
    login = group_login(server_roles=["dbcreator", "bulkadmin", "processadmin",
                                      "diskadmin"])
    assert _server_implied_level(login) == "none"


# --- securityadmin ----------------------------------------------------------

@pytest.fixture
def securityadmin_rows():
    return access_for(group_login(server_roles=["securityadmin"]))


def test_securityadmin_yields_a_row_with_no_db_principal(securityadmin_rows):
    assert len(securityadmin_rows) == 1
    assert securityadmin_rows[0].level == "admin"


def test_securityadmin_is_named_in_via(securityadmin_rows):
    assert "securityadmin" in securityadmin_rows[0].via


def test_securityadmin_sees_all_pii(securityadmin_rows):
    assert len(securityadmin_rows[0].pii_tables) == 2


# --- CONTROL SERVER (a permission, not a role) ------------------------------

def test_control_server_is_detected():
    assert _server_implied_level(group_login(server_permissions=["CONTROL SERVER"])) == "admin"


def test_control_server_matching_is_case_and_space_tolerant():
    login = Login(name="X", server_permissions=["  control server "])
    assert _server_implied_level(login) == "admin"


@pytest.fixture
def control_server_rows():
    return access_for(group_login(server_permissions=["CONTROL SERVER"]))


def test_control_server_yields_an_admin_row(control_server_rows):
    assert len(control_server_rows) == 1
    assert control_server_rows[0].level == "admin"
    assert len(control_server_rows[0].pii_tables) == 2


def test_control_server_is_named_in_via(control_server_rows):
    assert "CONTROL SERVER" in control_server_rows[0].via


def test_control_server_is_listed_as_a_grantor():
    login = group_login(server_permissions=["CONTROL SERVER"])
    assert _server_grantors(login) == ["CONTROL SERVER"]


def test_unrelated_server_permissions_imply_nothing():
    login = Login(name="X", server_permissions=["VIEW SERVER STATE", "CONNECT SQL"])
    assert _server_implied_level(login) == "none"


# --- CONTROL on the DATABASE (class 0) --------------------------------------

def test_the_scoped_query_covers_class_0():
    assert "class IN (0" in DB_SCOPED_PERMISSIONS_SQL


def test_the_server_permission_query_covers_class_100():
    assert "class IN (100" in SERVER_PERMISSIONS_SQL


@pytest.fixture
def db_control_rows():
    scoped = [Row(principal_name=GROUP, permission_name="CONTROL", state_desc="GRANT")]
    return access_for(group_login(), principals=PRINCIPALS, scoped=scoped)


def test_database_control_yields_an_admin_row(db_control_rows):
    assert len(db_control_rows) == 1
    assert db_control_rows[0].level == "admin"


def test_database_control_exposes_all_pii_in_that_database(db_control_rows):
    assert len(db_control_rows[0].pii_tables) == 2


def test_database_control_is_not_marked_server_wide(db_control_rows):
    assert "server-wide" not in db_control_rows[0].via


def test_database_scoped_select_grants_read_over_the_whole_database():
    scoped = [Row(principal_name=GROUP, permission_name="SELECT", state_desc="GRANT")]
    rows = access_for(group_login(), principals=PRINCIPALS, scoped=scoped)
    assert rows[0].level == "read"
    assert len(rows[0].pii_tables) == 2


# --- DENY must never confer -------------------------------------------------

def test_a_denied_database_control_confers_nothing():
    scoped = [Row(principal_name=GROUP, permission_name="CONTROL", state_desc="DENY")]
    rows = access_for(group_login(), principals=PRINCIPALS, scoped=scoped)
    assert rows[0].level == "none"
    assert rows[0].pii_tables == []


def test_a_denied_control_server_is_not_collected():
    logins = collect_server_logins(FakeCursor(
        logins=[Row(name=GROUP, type_desc="WINDOWS_GROUP", is_disabled=0)],
        server_perms=[Row(principal_name=GROUP, permission_name="CONTROL SERVER",
                          state_desc="DENY")]))
    assert logins[0].server_permissions == []


# --- collection wires both sources onto the Login ---------------------------

@pytest.fixture
def wired_login():
    logins = collect_server_logins(FakeCursor(
        logins=[Row(name=GROUP, type_desc="WINDOWS_GROUP", is_disabled=0)],
        server_roles=[Row(role_name="securityadmin", member_name=GROUP)],
        server_perms=[Row(principal_name=GROUP, permission_name="CONTROL SERVER",
                          state_desc="GRANT")]))
    return logins[0]


def test_server_roles_are_populated(wired_login):
    assert wired_login.server_roles == ["securityadmin"]


def test_server_permissions_are_populated(wired_login):
    assert wired_login.server_permissions == ["CONTROL SERVER"]


def test_both_are_reported_as_grantors(wired_login):
    assert _server_grantors(wired_login) == ["securityadmin", "CONTROL SERVER"]


# --- a rights failure degrades, never breaks --------------------------------

def test_logins_are_still_collected_when_the_new_query_fails():
    logins = collect_server_logins(Exploding())
    assert logins[0].name == GROUP
    assert logins[0].server_permissions == []


def test_db_collection_survives_a_scoped_permission_failure():
    rows = collect_db_access(Exploding(), "srv", "AppDB",
                             [group_login(server_roles=["sysadmin"])], PII)
    assert rows[0].level == "admin"


# --- no regression ----------------------------------------------------------

def test_a_plain_group_with_nothing_implicit_is_skipped():
    assert access_for(group_login(server_roles=["dbcreator"])) == []
