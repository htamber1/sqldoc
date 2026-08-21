"""Regression tests for login-less database principals (v3.1.0, patch 07).

Found auditing a live dev SQL Server in an estate that grants through AD role
groups. A Windows user or group can be granted directly in a database with NO
server login (`CREATE USER [DOMAIN\\Group]` with no `FOR LOGIN`). SQL Server
authorises those principals by SID from the connecting session's Windows token,
so they confer real access -- but `collect_db_access` seeded its loop from
`matched_logins`, so such a principal could never be its subject. `access check`
reported "no access" for a principal that held db_datareader, and the grant
generator then produced a redundant grant.

Measured on the dev estate: ~1,387 role-holding principals with no server login
across 4 servers / 69 databases -- 815 Windows groups, 480 Windows users, 202
orphaned SQL users. Not 6, and not only groups.

Three classes must be handled differently:
  * Windows user/group, auth WINDOWS, no login -> LIVE access (report it)
  * SQL user, auth INSTANCE, no login          -> broken orphan (different fix)
  * any principal, auth NONE                   -> deliberate WITHOUT LOGIN, never flag

Run with:  pytest test_db_principal_fixes.py
"""
import pytest

from sqldoc.access.checker import check_access
from sqldoc.access.model import AccessReport, DatabaseAccess, Login
from sqldoc.access.render import build_check_json, render_check_html
from sqldoc.access.review import review_db_principals
from sqldoc.access.script import pick_login
from sqldoc.access.sqlserver import (
    collect_db_access, collect_db_principals, login_less_windows_principals,
    principal_matches)


# --- fakes -----------------------------------------------------------------

class Row(dict):
    """A cursor row addressed by column name, like sqldoc.dbutil.cell expects."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)

    def keys(self):
        return list(super().keys())


class FakeCursor:
    """Dispatches on the /* MARKER */ comment each query carries."""

    def __init__(self, tables, fail_markers=()):
        self.tables = tables
        self.fail_markers = set(fail_markers)
        self._rows = []
        self.description = None
        self.executed = []

    def _marker(self, sql):
        for line in sql.splitlines():
            line = line.strip()
            if line.startswith("/*") and line.endswith("*/"):
                return line[2:-2].strip()
        return ""

    def execute(self, sql, *a):
        marker = self._marker(sql)
        self.executed.append(marker)
        if marker in self.fail_markers:
            raise RuntimeError(f"permission denied reading {marker}")
        self._rows = [Row(r) for r in self.tables.get(marker, [])]
        self.description = [(k,) for k in (self._rows[0].keys() if self._rows else [])]
        return self

    def fetchall(self):
        return list(self._rows)


class FakeUser:
    def __init__(self, identifier, login=None, sam=None, groups=None):
        self.identifier = identifier
        self.login = login if login is not None else identifier
        self.sam_account_name = sam if sam is not None else identifier
        self.groups = groups or []
        self.found = True
        self.source = "ldap"
        self.title = ""
        self.department = ""
        self.display_name = identifier
        self.email = ""
        self.enabled = True


DOM = "DOM"
GRP_P = rf"{DOM}\GRP-App_BA-P"        # group, NO server login
GRP_E = rf"{DOM}\GRP-App_BA-E"        # group, HAS a server login
GRP_BI_E = rf"{DOM}\GRP-Rpt_DEV-E"       # an "-E" group that has NO login
USR_NL = rf"{DOM}\jdoe"             # individual user, NO server login
SQL_ORPHAN = "LegacyRpt"             # SQL user, auth INSTANCE, no login
WITHOUT_LOGIN = "cdc"                    # auth NONE -- deliberate


def principals_table():
    """The `AppDB` shape, verified read-only against the real dev server."""
    return [
        {"db_user": GRP_E, "type_desc": "WINDOWS_GROUP",
         "auth_type": "WINDOWS", "has_server_login": 1},
        {"db_user": GRP_P, "type_desc": "WINDOWS_GROUP",
         "auth_type": "WINDOWS", "has_server_login": 0},
        {"db_user": GRP_BI_E, "type_desc": "WINDOWS_GROUP",
         "auth_type": "WINDOWS", "has_server_login": 0},
        {"db_user": USR_NL, "type_desc": "WINDOWS_USER",
         "auth_type": "WINDOWS", "has_server_login": 0},
        {"db_user": SQL_ORPHAN, "type_desc": "SQL_USER",
         "auth_type": "INSTANCE", "has_server_login": 0},
        {"db_user": WITHOUT_LOGIN, "type_desc": "SQL_USER",
         "auth_type": "NONE", "has_server_login": 0},
    ]


def base_tables(role_rows=None):
    return {
        "ACCESS_DB_PRINCIPALS": [{"db_user": p["db_user"], "type_desc": p["type_desc"]}
                                 for p in principals_table()],
        "ACCESS_DB_PRINCIPALS_DETAIL": principals_table(),
        "ACCESS_DB_ROLE_MEMBERS": role_rows if role_rows is not None else [
            {"member_name": GRP_E, "role_name": "db_datareader"},
            {"member_name": GRP_E, "role_name": "db_datawriter"},
            {"member_name": GRP_P, "role_name": "db_datareader"},
            {"member_name": USR_NL, "role_name": "db_datareader"},
            {"member_name": SQL_ORPHAN, "role_name": "db_datareader"},
            {"member_name": WITHOUT_LOGIN, "role_name": "db_owner"},
        ],
        "ACCESS_DB_PERMISSIONS": [],
        "ACCESS_DB_SCOPED_PERMISSIONS": [],
    }


# --- 1. the live class: a login-less group is real access ------------------

def test_login_less_group_principal_is_reported_as_access():
    """The core bug: asking about a group that exists only as a database
    principal previously returned nothing at all."""
    cur = FakeCursor(base_tables())
    user = FakeUser(GRP_P)
    rows = collect_db_access(cur, "dev-sql-1", "AppDB", [], None, user=user)
    assert len(rows) == 1
    a = rows[0]
    assert a.principal == GRP_P
    assert a.principal_type == "WINDOWS_GROUP"
    assert a.has_server_login is False
    assert a.login == ""                      # never fabricated
    assert a.db_user == GRP_P
    assert a.roles == ["db_datareader"]
    assert a.level == "read"
    assert "no server login" in a.via


def test_login_less_individual_user_principal_is_reported():
    """480 of the ~1,387 measured are individual Windows users, not groups."""
    cur = FakeCursor(base_tables())
    user = FakeUser(rf"{DOM}\jdoe", sam="jdoe")
    rows = collect_db_access(cur, "dev-sql-1", "AppDB", [], None, user=user)
    assert [r.principal for r in rows] == [USR_NL]
    assert rows[0].principal_type == "WINDOWS_USER"
    assert rows[0].has_server_login is False


def test_detection_is_by_linkage_not_by_the_P_name_convention():
    """`-P` is a naming convention, not the mechanism: an `-E` group can be
    login-less too, and an `-E` group with a login must NOT be reported."""
    live = login_less_windows_principals(
        collect_db_principals(FakeCursor(base_tables())))
    assert GRP_BI_E in live               # "-E" but has no login
    assert GRP_P in live
    assert GRP_E not in live              # "-P"-less but HAS a login


# --- 2. the broken class and the deliberate class --------------------------

def test_orphaned_sql_user_is_not_reported_as_user_access():
    """A login-less SQL user is broken, not access. It must not appear here."""
    live = login_less_windows_principals(
        collect_db_principals(FakeCursor(base_tables())))
    assert SQL_ORPHAN not in live


def test_without_login_user_is_never_treated_as_live_access():
    """auth=NONE is a deliberate CREATE USER ... WITHOUT LOGIN (module signing,
    EXECUTE AS). Flagging it is a guaranteed false positive."""
    live = login_less_windows_principals(
        collect_db_principals(FakeCursor(base_tables())))
    assert WITHOUT_LOGIN not in live


def test_without_login_user_is_never_a_review_finding():
    cur = FakeCursor(base_tables())
    findings = review_db_principals(cur, "dev-sql-1", "AppDB", logins=[],
                                    include_read_principals=True)
    assert WITHOUT_LOGIN not in [f.principal for f in findings]


# --- 3. the review finding: three categories, tiered severity --------------

def _review(level_roles, include_read=False, logins=None):
    tables = base_tables(role_rows=[{"member_name": GRP_P, "role_name": r}
                                    for r in level_roles])
    return review_db_principals(FakeCursor(tables), "dev-sql-1", "AppDB",
                                logins=logins or [],
                                include_read_principals=include_read)


def test_read_level_principal_is_suppressed_by_default():
    """~1,180 live login-less Windows principals on 4 dev servers. Emitting the
    read-level ones by default would bury every real finding."""
    assert _review(["db_datareader"]) == []


def test_read_level_principal_is_reported_when_opted_in():
    findings = _review(["db_datareader"], include_read=True)
    assert [f.category for f in findings] == ["db_principal_no_login"]
    assert findings[0].severity == "LOW"


def test_write_level_principal_is_medium():
    findings = _review(["db_datawriter"])
    assert findings[0].category == "db_principal_no_login"
    assert findings[0].severity == "MEDIUM"


def test_admin_level_principal_is_high():
    """A db_owner with no server login is exactly what must not be suppressed."""
    findings = _review(["db_owner"])
    assert findings[0].severity == "HIGH"


def test_admin_principal_fix_sql_does_not_drop_blindly():
    """Dropping a live group principal revokes access from every member."""
    sql = _review(["db_owner"])[0].fix_sql
    assert "REVIEW ONLY" in sql
    for line in sql.splitlines():
        if "DROP USER" in line:
            assert line.strip().startswith("--"), f"uncommented DROP: {line}"


def test_orphaned_sql_user_is_its_own_category():
    findings = review_db_principals(FakeCursor(base_tables()), "dev-sql-1", "AppDB",
                                    logins=[], include_read_principals=True)
    orphans = [f for f in findings if f.category == "orphaned_db_user"]
    assert [f.principal for f in orphans] == [SQL_ORPHAN]
    assert orphans[0].severity == "MEDIUM"
    assert "ALTER USER" in orphans[0].fix_sql


def test_sql_user_with_a_matching_login_is_not_an_orphan():
    """`has_login` is by SID; the name check guards the case where a login of
    that name does exist and only the linkage query is coarse."""
    findings = review_db_principals(
        FakeCursor(base_tables()), "dev-sql-1", "AppDB",
        logins=[Login(name=SQL_ORPHAN, type="SQL_LOGIN")],
        include_read_principals=True)
    assert "orphaned_db_user" not in [f.category for f in findings]


def test_builtin_principals_are_not_flagged():
    tables = base_tables(role_rows=[{"member_name": r"NT SERVICE\MSSQLSERVER",
                                     "role_name": "db_owner"}])
    tables["ACCESS_DB_PRINCIPALS_DETAIL"] = [
        {"db_user": r"NT SERVICE\MSSQLSERVER", "type_desc": "WINDOWS_USER",
         "auth_type": "WINDOWS", "has_server_login": 0}]
    findings = review_db_principals(FakeCursor(tables), "dev-sql-1", "AppDB",
                                    logins=[], include_read_principals=True)
    assert findings == []


def test_principal_with_no_entitlements_is_not_reported():
    tables = base_tables(role_rows=[])
    findings = review_db_principals(FakeCursor(tables), "dev-sql-1", "AppDB",
                                    logins=[], include_read_principals=True)
    assert findings == []


# --- 4. graceful degradation ----------------------------------------------

def test_unreadable_linkage_degrades_to_login_only_behaviour():
    """If the enriched query cannot be read, report nothing new rather than
    failing the audit -- the established _fetch idiom."""
    cur = FakeCursor(base_tables(), fail_markers={"ACCESS_DB_PRINCIPALS_DETAIL"})
    assert collect_db_principals(cur) == {}
    rows = collect_db_access(cur, "dev-sql-1", "AppDB", [], None, user=FakeUser(GRP_P))
    assert rows == []
    assert review_db_principals(cur, "dev-sql-1", "AppDB", logins=[],
                                include_read_principals=True) == []


def test_signature_stays_backward_compatible_without_user():
    """Omitting `user` must reproduce the previous login-only behaviour."""
    cur = FakeCursor(base_tables())
    assert collect_db_access(cur, "dev-sql-1", "AppDB", [], None) == []


# --- 5. no double-counting, and login-backed rows still work ---------------

def test_login_backed_access_still_reported_and_flagged_as_such():
    cur = FakeCursor(base_tables())
    lg = Login(name=GRP_E, type="WINDOWS_GROUP")
    user = FakeUser("someone", sam="someone", groups=[GRP_E])
    rows = collect_db_access(cur, "dev-sql-1", "AppDB", [lg], None, user=user)
    assert len(rows) == 1
    assert rows[0].has_server_login is True
    assert rows[0].login == GRP_E
    assert rows[0].principal == GRP_E
    assert rows[0].level == "write"


def test_principal_matched_by_both_paths_is_not_duplicated():
    """A group with a login is matched as a login; it must not also be emitted
    by the principal pass."""
    tables = base_tables()
    tables["ACCESS_DB_PRINCIPALS_DETAIL"] = [
        dict(p, has_server_login=1) if p["db_user"] == GRP_E else p
        for p in principals_table()]
    cur = FakeCursor(tables)
    lg = Login(name=GRP_E, type="WINDOWS_GROUP")
    user = FakeUser(GRP_E, groups=[GRP_E])
    rows = collect_db_access(cur, "dev-sql-1", "AppDB", [lg], None, user=user)
    assert [r.principal for r in rows] == [GRP_E]


def test_non_matching_principals_are_not_returned():
    """Only principals this identifier actually holds."""
    cur = FakeCursor(base_tables())
    user = FakeUser(rf"{DOM}\someone_else", sam="someone_else")
    assert collect_db_access(cur, "dev-sql-1", "AppDB", [], None, user=user) == []


# --- 6. the shared predicate ----------------------------------------------

def test_principal_matches_is_the_same_predicate_for_groups_and_logins():
    user = FakeUser("jsmith", login=rf"{DOM}\jsmith", sam="jsmith", groups=[GRP_P])
    assert principal_matches(GRP_P, "WINDOWS_GROUP", user)
    assert principal_matches(rf"{DOM}\jsmith", "WINDOWS_USER", user)
    assert not principal_matches(GRP_E, "WINDOWS_GROUP", user)


# --- 7. matched_groups counts access, not logins ---------------------------

class _Adapter:
    def __init__(self, cur):
        self._cur = cur

    def extract_metadata(self):
        return []

    def connect(self):
        return self

    def cursor(self, _conn):
        return self._cur

    def close(self):
        pass


class _Source:
    def __init__(self, user):
        self._user = user

    def get_user(self, _ident):
        return self._user


def _check(user, tables):
    cur = FakeCursor(dict(tables, ACCESS_SERVER_LOGINS=[],
                          ACCESS_SERVER_ROLE_MEMBERS=[], ACCESS_SERVER_PERMISSIONS=[]))
    cfg = {"access": {"servers": [{"name": "dev-sql-1", "server": "dev-sql-1",
                                   "databases": ["AppDB"]}]}}
    return check_access(cfg, user.identifier, source=_Source(user),
                        adapter_factory=lambda e, d: _Adapter(cur))


def test_matched_groups_counts_a_login_less_group():
    """Previously printed "with SQL access: 0" for a user holding db_datareader."""
    report = _check(FakeUser("jsmith", sam="jsmith", groups=[GRP_P]), base_tables())
    assert report.matched_groups == [GRP_P]
    assert report.has_any_access()
    assert report.logins == []          # correct: there IS no login


def test_login_less_user_principal_does_not_become_a_matched_group():
    report = _check(FakeUser(rf"{DOM}\jdoe", sam="jdoe"), base_tables())
    assert report.matched_groups == []
    assert [a.principal for a in report.access] == [USR_NL]


# --- 8. the grant generator must not materialise a login -------------------

class _Parsed:
    def __init__(self, database, level="write"):
        self.database = database
        self.level = level
        self.schema = ""
        self.raw = ""


def test_pick_login_never_targets_a_login_less_principal():
    """Returning it would emit CREATE LOGIN [group] FROM WINDOWS, giving a
    deliberately login-less group a server login as a side effect of a grant."""
    report = AccessReport(user=FakeUser("jsmith", sam="jsmith", groups=[GRP_P]))
    report.access = [DatabaseAccess(
        server="dev-sql-1", database="AppDB", login="", db_user=GRP_P,
        via=f"group {GRP_P} (database principal; no server login)",
        roles=["db_datareader"], level="read",
        principal=GRP_P, principal_type="WINDOWS_GROUP", has_server_login=False)]
    login, is_group, note = pick_login(report, _Parsed("AppDB"))
    assert login != ""
    assert login == rf"{DOM}\jsmith" or login == "jsmith"


def test_pick_login_still_prefers_a_real_group_login():
    report = AccessReport(user=FakeUser("jsmith", sam="jsmith", groups=[GRP_E]))
    report.access = [
        DatabaseAccess(server="dev-sql-1", database="AppDB", login="", db_user=GRP_P,
                       via=f"group {GRP_P} (database principal; no server login)",
                       roles=["db_datareader"], level="read", principal=GRP_P,
                       principal_type="WINDOWS_GROUP", has_server_login=False),
        DatabaseAccess(server="dev-sql-1", database="AppDB", login=GRP_E, db_user=GRP_E,
                       via=f"group {GRP_E}", roles=["db_datareader"], level="read",
                       principal=GRP_E, principal_type="WINDOWS_GROUP",
                       has_server_login=True),
    ]
    login, is_group, note = pick_login(report, _Parsed("AppDB"))
    assert login == GRP_E and is_group is True


# --- 9. report contract: additive only ------------------------------------

def test_check_json_keys_are_additive_and_existing_ones_unchanged():
    report = _check(FakeUser("jsmith", sam="jsmith", groups=[GRP_P]), base_tables())
    data = build_check_json(report)
    assert set(data) == {"report_type", "user", "matched_groups", "logins",
                         "access", "errors"}
    row = data["access"][0]
    # every pre-existing key still present, same meaning
    for k in ("server", "database", "login", "db_user", "via", "roles", "level",
              "permissions", "pii_tables", "flags"):
        assert k in row
    # the three new ones
    assert row["principal"] == GRP_P
    assert row["principal_type"] == "WINDOWS_GROUP"
    assert row["has_server_login"] is False
    assert row["login"] == ""


def test_check_html_marks_the_login_less_principal(tmp_path):
    report = _check(FakeUser("jsmith", sam="jsmith", groups=[GRP_P]), base_tables())
    out = tmp_path / "check.html"
    render_check_html(report, str(out))
    html = out.read_text(encoding="utf-8")
    assert "no server login" in html
    assert GRP_P in html


# --- `include_read_principals` is settable from .sqldoc.yml, not just the CLI -----
#
# The flag was added to the CLI but not to CONFIG_KEYS, so `load_config` discarded it
# with an "unknown config key" warning. Adding it to CONFIG_KEYS is only half the wiring:
# `access review` resolves its own options instead of using the generic resolver, so the
# value would have loaded and then been ignored -- the collected-but-never-surfaced
# pattern this whole release exists to remove.

class TestIncludeReadPrincipalsIsConfigurable:

    def _cfg(self, tmp_path, body):
        p = tmp_path / ".sqldoc.yml"
        p.write_text(body, encoding="utf-8")
        return str(p)

    def test_key_is_accepted_by_config_loader(self, tmp_path):
        """Not in CONFIG_KEYS -> load_config drops it and warns."""
        from sqldoc.cli import CONFIG_KEYS
        assert "include_read_principals" in CONFIG_KEYS

    def test_top_level_key_survives_load_config(self, tmp_path):
        from sqldoc.cli import load_config
        cfg = load_config(self._cfg(tmp_path, "include_read_principals: true\n"), True)
        assert cfg.get("include_read_principals") is True

    def test_hyphenated_spelling_also_works(self, tmp_path):
        """load_config normalises '-' to '_', so both spellings must land."""
        from sqldoc.cli import load_config
        cfg = load_config(self._cfg(tmp_path, "include-read-principals: true\n"), True)
        assert cfg.get("include_read_principals") is True

    def test_config_value_actually_reaches_review_access(self, tmp_path, monkeypatch):
        """The half-wiring guard: the value must change behaviour, not just load."""
        from click.testing import CliRunner
        from sqldoc import cli as cli_mod

        seen = {}

        def fake_review_access(cfg, inactive_days=None, include_read_principals=False):
            seen["flag"] = include_read_principals
            return []

        monkeypatch.setattr("sqldoc.access.review.review_access", fake_review_access)
        cfgp = self._cfg(tmp_path, "include_read_principals: true\n")
        res = CliRunner().invoke(cli_mod.cli, [
            "access", "review", "--config", cfgp,
            "--output", str(tmp_path / "r.html")])
        assert res.exit_code == 0, res.output
        assert seen.get("flag") is True, "config value loaded but never reached review_access"

    def test_access_review_section_also_works(self, tmp_path, monkeypatch):
        """The section `inactive_days` already lives in is honoured too."""
        from click.testing import CliRunner
        from sqldoc import cli as cli_mod

        seen = {}

        def fake_review_access(cfg, inactive_days=None, include_read_principals=False):
            seen["flag"] = include_read_principals
            return []

        monkeypatch.setattr("sqldoc.access.review.review_access", fake_review_access)
        cfgp = self._cfg(tmp_path, "access:\n  review:\n    include_read_principals: true\n")
        res = CliRunner().invoke(cli_mod.cli, [
            "access", "review", "--config", cfgp,
            "--output", str(tmp_path / "r.html")])
        assert res.exit_code == 0, res.output
        assert seen.get("flag") is True

    def test_defaults_off_when_absent(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from sqldoc import cli as cli_mod

        seen = {}

        def fake_review_access(cfg, inactive_days=None, include_read_principals=False):
            seen["flag"] = include_read_principals
            return []

        monkeypatch.setattr("sqldoc.access.review.review_access", fake_review_access)
        cfgp = self._cfg(tmp_path, "inactive_days: 30\n")
        res = CliRunner().invoke(cli_mod.cli, [
            "access", "review", "--config", cfgp,
            "--output", str(tmp_path / "r.html")])
        assert res.exit_code == 0, res.output
        assert seen.get("flag") is False, "read-level principals must stay off by default"

    def test_explicit_flag_still_wins_over_absent_config(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from sqldoc import cli as cli_mod

        seen = {}

        def fake_review_access(cfg, inactive_days=None, include_read_principals=False):
            seen["flag"] = include_read_principals
            return []

        monkeypatch.setattr("sqldoc.access.review.review_access", fake_review_access)
        cfgp = self._cfg(tmp_path, "inactive_days: 30\n")
        res = CliRunner().invoke(cli_mod.cli, [
            "access", "review", "--config", cfgp, "--include-read-principals",
            "--output", str(tmp_path / "r.html")])
        assert res.exit_code == 0, res.output
        assert seen.get("flag") is True
