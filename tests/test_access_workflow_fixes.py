"""The access WORKFLOW commands, once server-wide privilege became visible.

Once `collect_db_access` started returning rows for logins with no database
principal (a sysadmin has none), three downstream commands were wrong. NOTES
Part 3 class I / J; the field patch shipped no cover for these.

  * `gap.py`    -- an access row derived from server-wide privilege has no roles
                   and no object permissions, so the "current access" list came
                   back EMPTY while the verdict said the user already had access.
  * `script.py` -- the grantee selector chose the SYSADMIN GROUP as the target of
                   a db_datareader grant: redundant for the requester, and it
                   widens a privileged group's footprint for every other member.
  * `review.py` -- `NT AUTHORITY\\SYSTEM` and `NT SERVICE\\*` were flagged HIGH
                   "orphaned -- no backing AD account", because the check resolves
                   the name in AD and virtual accounts can never exist there. The
                   generated fix was `DROP LOGIN [NT SERVICE\\MSSQLSERVER]` --
                   following it would break the SQL Server service. 6 of 25
                   findings live were this false positive.

No database or directory required.
"""
import pytest

from sqldoc.access.gap import analyze_gap
from sqldoc.access.model import (ADUser, AccessReport, DatabaseAccess, Login,
                                 ParsedRequest)
from sqldoc.access.review import is_builtin_principal, review_logins
from sqldoc.access.script import pick_login
from sqldoc.access.sqlserver import _opt

SYSADMIN_VIA = "group DOM\\D-Admins -> server-wide sysadmin"


def server_wide_report():
    """A user whose only access comes from a sysadmin group login: no database
    role, no object permission, nothing to name but the resolution path."""
    report = AccessReport(user=ADUser(identifier="DOM\\jsmith",
                                      display_name="J Smith",
                                      login="DOM\\jsmith"))
    report.logins.append(Login(name="DOM\\D-Admins", type="WINDOWS_GROUP",
                               server_roles=["sysadmin"]))
    report.access.append(DatabaseAccess(
        server="srv", database="AppDB", login="DOM\\D-Admins", db_user="",
        via=SYSADMIN_VIA, level="admin"))
    return report


# --- gap.py: current access must not come back empty ------------------------

@pytest.fixture
def server_wide_gap():
    return analyze_gap(ParsedRequest(raw="read on AppDB", database="AppDB",
                                     level="read"),
                       server_wide_report())


def test_server_wide_privilege_is_verdict_already(server_wide_gap):
    assert server_wide_gap.verdict == "ALREADY"


def test_server_wide_privilege_lists_current_access(server_wide_gap):
    """The bug: verdict said ALREADY while `current` was empty, so the report
    asserted access while showing no evidence for it."""
    assert server_wide_gap.current != []
    assert any("admin" in item for item in server_wide_gap.current)


def test_current_access_names_the_resolution_path(server_wide_gap):
    assert any(SYSADMIN_VIA in item for item in server_wide_gap.current)


def test_explanation_cites_the_path_when_there_is_no_role(server_wide_gap):
    assert SYSADMIN_VIA in server_wide_gap.explanation


def test_a_role_backed_row_still_cites_its_roles():
    """No regression: when there IS a database role, name that, not the path."""
    report = AccessReport(user=ADUser(identifier="u", display_name="U"))
    report.access.append(DatabaseAccess(
        server="srv", database="AppDB", login="DOM\\D-Readers",
        db_user="DOM\\D-Readers", via="group DOM\\D-Readers",
        roles=["db_datareader"], level="read"))
    result = analyze_gap(ParsedRequest(raw="read on AppDB", database="AppDB",
                                       level="read"), report)
    assert result.verdict == "ALREADY"
    assert "db_datareader" in result.explanation


# --- script.py: never grant to a group that already satisfies the level -----

def test_grantee_skips_a_group_that_already_has_the_level():
    login, uses_group, note = pick_login(server_wide_report(),
                                         ParsedRequest(raw="r", database="AppDB",
                                                       level="read"))
    assert login == "DOM\\jsmith"
    assert uses_group is False
    assert "no suitable AD group" in note


def test_grantee_still_prefers_a_group_that_does_not_have_the_level():
    """No regression: an under-privileged group in the target database is still
    the preferred grantee -- that is the whole point of the tier."""
    report = AccessReport(user=ADUser(identifier="DOM\\jsmith", login="DOM\\jsmith"))
    report.access.append(DatabaseAccess(
        server="srv", database="AppDB", login="DOM\\D-Readers",
        via="group DOM\\D-Readers", roles=["db_datareader"], level="read"))
    login, uses_group, _note = pick_login(
        report, ParsedRequest(raw="w", database="AppDB", level="write"))
    assert login == "DOM\\D-Readers"
    assert uses_group is True


def test_grantee_skips_a_server_wide_privileged_group_login():
    """Tier 2: a group login the user belongs to that is already sysadmin cannot
    be added to by the requested level."""
    report = AccessReport(user=ADUser(identifier="DOM\\jsmith", login="DOM\\jsmith"))
    report.logins.append(Login(name="DOM\\D-Admins", type="WINDOWS_GROUP",
                               server_roles=["sysadmin"]))
    login, uses_group, _note = pick_login(
        report, ParsedRequest(raw="r", database="AppDB", level="read"))
    assert login == "DOM\\jsmith"
    assert uses_group is False


def test_grantee_uses_an_unprivileged_group_login():
    report = AccessReport(user=ADUser(identifier="DOM\\jsmith", login="DOM\\jsmith"))
    report.logins.append(Login(name="DOM\\D-Apps", type="WINDOWS_GROUP",
                               server_roles=["dbcreator"]))
    login, uses_group, _note = pick_login(
        report, ParsedRequest(raw="r", database="AppDB", level="read"))
    assert login == "DOM\\D-Apps"
    assert uses_group is True


def test_an_explicit_override_still_wins():
    login, uses_group, note = pick_login(
        server_wide_report(), ParsedRequest(raw="r", database="AppDB", level="read"),
        override="DOM\\someone")
    assert login == "DOM\\someone"
    assert uses_group is True
    assert note == "caller-specified login"


# --- review.py: built-in Windows authorities are not AD identities ----------

@pytest.mark.parametrize("name", [
    "NT AUTHORITY\\SYSTEM",
    "NT AUTHORITY\\NETWORK SERVICE",
    "NT SERVICE\\MSSQLSERVER",
    "NT SERVICE\\SQLSERVERAGENT",
    "BUILTIN\\Administrators",
    "NT VIRTUAL MACHINE\\vm-guid",
    "IIS APPPOOL\\DefaultAppPool",
    "nt service\\mssqlserver",          # case-insensitive
    "  NT SERVICE\\SQLWriter",          # tolerates surrounding space
])
def test_builtin_principals_are_recognised(name):
    assert is_builtin_principal(name) is True


@pytest.mark.parametrize("name", [
    "CORP\\jsmith",
    "CORP\\D-Admins",
    "sa",                                # no domain part at all
    "",
    None,
])
def test_real_identities_are_not_treated_as_builtin(name):
    assert is_builtin_principal(name) is False


class FakeCursor:
    def __init__(self, data):
        self.data, self._rows = data, []

    def execute(self, sql, *a):
        self._rows = []
        for token, rows in self.data.items():
            if token in sql:
                self._rows = rows
                return

    def fetchall(self):
        return self._rows


class NeverFoundSource:
    """A directory in which nothing resolves -- the worst case for the orphan
    check, and exactly what a virtual account looks like to it."""

    def get_user(self, name):
        return ADUser(identifier=name, found=False)


NOW = 1_800_000_000.0

BUILTIN_DATA = {
    "ACCESS_SERVER_LOGINS": [
        {"name": "NT SERVICE\\MSSQLSERVER", "type_desc": "WINDOWS_LOGIN", "is_disabled": 0},
        {"name": "NT AUTHORITY\\SYSTEM", "type_desc": "WINDOWS_LOGIN", "is_disabled": 0},
        {"name": "BUILTIN\\Administrators", "type_desc": "WINDOWS_GROUP", "is_disabled": 0},
        {"name": "CORP\\ghost", "type_desc": "WINDOWS_LOGIN", "is_disabled": 0},
    ],
    # No activity rows at all, so every login also looks inactive.
    "ACCESS_LOGIN_ACTIVITY": [],
}


@pytest.fixture
def builtin_findings():
    return review_logins(FakeCursor(BUILTIN_DATA), "prod", NeverFoundSource(),
                         inactive_days=90, now_epoch=NOW)


def test_builtin_logins_are_never_flagged_orphaned(builtin_findings):
    flagged = {f.principal for f in builtin_findings if f.category == "orphaned"}
    assert "NT SERVICE\\MSSQLSERVER" not in flagged
    assert "NT AUTHORITY\\SYSTEM" not in flagged
    assert "BUILTIN\\Administrators" not in flagged


def test_no_fix_script_ever_drops_the_service_account(builtin_findings):
    """The original generated `DROP LOGIN [NT SERVICE\\MSSQLSERVER]` -- running
    it would break the instance."""
    for finding in builtin_findings:
        assert "NT SERVICE" not in (finding.fix_sql or "")
        assert "NT AUTHORITY" not in (finding.fix_sql or "")


def test_builtin_logins_are_not_flagged_inactive_either(builtin_findings):
    """The inactivity path would emit ALTER LOGIN ... DISABLE for the same
    accounts."""
    flagged = {f.principal for f in builtin_findings if f.category == "inactive"}
    assert not any(is_builtin_principal(p) for p in flagged)


def test_a_genuinely_orphaned_login_is_still_flagged(builtin_findings):
    """The skip must not blunt the check for real identities."""
    orphans = [f for f in builtin_findings if f.category == "orphaned"]
    assert [f.principal for f in orphans] == ["CORP\\ghost"]
    assert "DROP LOGIN" in orphans[0].fix_sql


# --- J. optional enrichment columns are read tolerantly ---------------------

def test_opt_returns_the_value_when_present():
    assert _opt({"perm_class": 100}, "perm_class") == 100


def test_opt_falls_back_when_the_column_is_absent():
    """An unexpected row shape must degrade to the un-enriched behaviour, not
    raise AttributeError mid-audit."""
    assert _opt({"permission_name": "CONTROL"}, "perm_class", 0) == 0
    assert _opt({}, "target_name") is None
