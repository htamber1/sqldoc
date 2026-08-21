"""Regression tests for the access-command group-principal fixes (v3.1.0).

Found running `access recommend` / `approve` / `review --cms` against a live
dev SQL Server in an estate that grants exclusively through AD role groups
(`DOMAIN\\GRP-App_QA`), never to individuals.

1. `match_user_logins` matched Windows-group logins only via `user.groups`, so
   an identifier that IS a group matched nothing: `access check` reported "no
   access" for the principal actually holding it, and `access script`/`approve`
   then generated a redundant grant.
2. `gather_peers` learned peers from service accounts (which routinely hold
   db_owner) while silently dropping group principals -- and reported a bare
   "0 peer(s)" that read as "nobody comparable exists".
3. `pick_login` told a group-based shop "no suitable AD group found -- consider
   creating a role-based AD group" while granting to their AD group.
4. The executive / access-review / cms-report estate paths printed
   `len(inv.servers)`, so a `--group` run announced the whole estate.

Run with:  pytest test_access_group_fixes.py
"""
import pytest

from sqldoc import cli as cli_mod
from sqldoc import cms as cms_mod
from sqldoc.access import recommend as recommend_mod
from sqldoc.access.script import pick_login
from sqldoc.access.sqlserver import match_user_logins


# --- fakes -----------------------------------------------------------------

class FakeLogin:
    def __init__(self, name, type_, server_roles=None):
        self.name = name
        self.type = type_
        self.is_disabled = False
        self.server_roles = server_roles or []
        self.server = ""


class FakeUser:
    def __init__(self, identifier, login=None, sam=None, groups=None):
        self.identifier = identifier
        self.login = login if login is not None else identifier
        self.sam_account_name = sam if sam is not None else identifier
        self.groups = groups or []
        self.found = True
        self.title = ""
        self.department = ""


GROUP = "WINDOWS_GROUP"
WUSER = "WINDOWS_LOGIN"


# --- 1. a Windows group identifier matches its own login -------------------

def test_group_identifier_matches_its_own_login():
    """The estate grants to `GRP-App_QA-E`; asking about that group by name
    must find the group's own server login."""
    logins = [FakeLogin(r"DOM\GRP-App_QA-E", GROUP),
              FakeLogin(r"DOM\GRP-Other_QA-E", GROUP),
              FakeLogin("applogin", "SQL_LOGIN")]
    user = FakeUser(r"DOM\GRP-App_QA-E")       # no AD groups resolved
    matched = match_user_logins(logins, user)
    assert [m.name for m in matched] == [r"DOM\GRP-App_QA-E"]


def test_group_identifier_matches_by_bare_name():
    logins = [FakeLogin(r"DOM\GRP-App_QA-E", GROUP)]
    user = FakeUser("GRP-App_QA-E", login="GRP-App_QA-E", sam="GRP-App_QA-E")
    assert len(match_user_logins(logins, user)) == 1


def test_membership_matching_still_works():
    """The original behaviour -- a person in a group -- is unchanged."""
    logins = [FakeLogin(r"DOM\GRP-App_QA-E", GROUP)]
    user = FakeUser(r"DOM\alice", groups=[r"DOM\GRP-App_QA-E"])
    assert len(match_user_logins(logins, user)) == 1


def test_unrelated_group_still_does_not_match():
    logins = [FakeLogin(r"DOM\GRP-Payroll_PRD-E", GROUP)]
    user = FakeUser(r"DOM\GRP-App_QA-E")
    assert match_user_logins(logins, user) == []


def test_blank_identifier_does_not_match_everything():
    """Empty identity fields must not become a wildcard."""
    logins = [FakeLogin(r"DOM\GRP-App_QA-E", GROUP), FakeLogin("", GROUP)]
    user = FakeUser("", login="", sam="")
    assert match_user_logins(logins, user) == []


def test_individual_login_matching_unchanged():
    logins = [FakeLogin(r"DOM\alice", WUSER), FakeLogin(r"DOM\bob", WUSER)]
    user = FakeUser(r"DOM\alice", sam=r"DOM\alice")
    assert [m.name for m in match_user_logins(logins, user)] == [r"DOM\alice"]


# --- 2. peer gathering excludes service accounts, counts what it skips ------

class _Cursor:
    """Returns principals for the principals query and roles for the roles query."""
    def __init__(self, principals, roles):
        self._principals = principals
        self._roles = roles
        self._rows = []

    def execute(self, sql, *a):
        s = " ".join(str(sql).split()).lower()
        self._rows = self._roles if "role" in s and "member" in s else self._principals
        return self

    def fetchall(self):
        return self._rows


class _Adapter:
    def __init__(self, cursor):
        self._c = cursor

    def connect(self):
        return self

    def cursor(self, conn):
        return self._c

    def close(self):
        pass


def _principal(name, type_desc):
    return {"db_user": name, "type_desc": type_desc}


def _role(member, role):
    return {"member_name": member, "role_name": role}


class _Source:
    """Directory that resolves every individual with a title."""
    class _U:
        found = True
        title = "Analyst"
        department = "ExampleDept"

    def get_user(self, name):
        return self._U()


def _gather(principals, roles):
    cfg = {"access": {"servers": [{"name": "s1", "server": "s1", "databases": ["AppDb"],
                                   "windows_auth": True}]}}
    cursor = _Cursor(principals, roles)
    stats = {}
    peers = recommend_mod.gather_peers(
        cfg, _Source(), adapter_factory=lambda entry, db: _Adapter(cursor), stats=stats)
    return peers, stats


def test_service_accounts_are_not_peers():
    """Learning least privilege from accounts holding db_owner defeats the point."""
    principals = [_principal(r"DOM\APPDB_SVC", "WINDOWS_USER"),
                  _principal(r"DOM\alice", "WINDOWS_USER")]
    roles = [_role(r"DOM\APPDB_SVC", "db_owner"), _role(r"DOM\alice", "db_datareader")]
    peers, stats = _gather(principals, roles)
    assert [p.login for p in peers] == [r"DOM\alice"]
    assert stats["service_accounts_skipped"] == 1


def test_group_principals_are_counted_not_silently_dropped():
    principals = [_principal(r"DOM\GRP-App_QA-E", "WINDOWS_GROUP"),
                  _principal(r"DOM\GRP-App_BA-E", "WINDOWS_GROUP")]
    roles = [_role(r"DOM\GRP-App_QA-E", "db_datareader")]
    peers, stats = _gather(principals, roles)
    assert peers == []
    assert stats["groups_skipped"] == 2, "a group-based estate must be told why it got 0 peers"


def test_builtin_principals_are_not_peers():
    principals = [_principal(r"NT AUTHORITY\SYSTEM", "WINDOWS_USER"),
                  _principal(r"DOM\alice", "WINDOWS_USER")]
    roles = [_role(r"DOM\alice", "db_datareader")]
    peers, _ = _gather(principals, roles)
    assert [p.login for p in peers] == [r"DOM\alice"]


def test_gather_peers_still_works_without_stats():
    """`stats` is optional -- existing callers must keep working."""
    cfg = {"access": {"servers": [{"name": "s1", "server": "s1", "databases": ["AppDb"],
                                   "windows_auth": True}]}}
    cursor = _Cursor([_principal(r"DOM\alice", "WINDOWS_USER")],
                     [_role(r"DOM\alice", "db_datareader")])
    peers = recommend_mod.gather_peers(cfg, _Source(),
                                       adapter_factory=lambda e, d: _Adapter(cursor))
    assert [p.login for p in peers] == [r"DOM\alice"]


# --- 3. pick_login names the group case correctly --------------------------

class FakeReport:
    def __init__(self, user, logins=None, access=None):
        self.user = user
        self.logins = logins or []
        self.access = access or []


class FakeParsed:
    def __init__(self, database, level):
        self.database = database
        self.level = level
        self.schema = None


def test_pick_login_grants_directly_to_the_requested_group():
    grp = r"DOM\GRP-App_QA-E"
    report = FakeReport(FakeUser(grp), logins=[FakeLogin(grp, GROUP)])
    login, uses_group, note = pick_login(report, FakeParsed("AppDB", "read"))
    assert login == grp
    assert uses_group is True
    assert "no suitable AD group found" not in note
    assert "requested AD group" in note


def test_pick_login_membership_case_keeps_its_wording():
    grp = r"DOM\GRP-App_QA-E"
    report = FakeReport(FakeUser(r"DOM\alice", groups=[grp]), logins=[FakeLogin(grp, GROUP)])
    login, uses_group, note = pick_login(report, FakeParsed("AppDB", "read"))
    assert login == grp and uses_group is True
    assert "belongs to" in note


def test_pick_login_individual_fallback_unchanged():
    report = FakeReport(FakeUser(r"DOM\alice"), logins=[FakeLogin(r"DOM\alice", WUSER)])
    login, uses_group, note = pick_login(report, FakeParsed("AppDB", "read"))
    assert login == r"DOM\alice"
    assert uses_group is False
    assert "no suitable AD group found" in note


# --- 4. estate paths report the scoped server count ------------------------

def _inventory():
    inv = cms_mod.CmsInventory(cms_server="cms")
    inv.groups = [cms_mod.CmsGroup(id=1, name="DevGroup", parent_id=None, path="DevGroup"),
                  cms_mod.CmsGroup(id=2, name="Other", parent_id=None, path="Other")]
    inv.servers = [
        cms_mod.CmsServer(name="d1", server_name="d1", group_id=1, group_path="DevGroup"),
        cms_mod.CmsServer(name="d2", server_name="d2", group_id=1, group_path="DevGroup"),
        cms_mod.CmsServer(name="x1", server_name="x1", group_id=2, group_path="Other"),
        cms_mod.CmsServer(name="x2", server_name="x2", group_id=2, group_path="Other"),
        cms_mod.CmsServer(name="x3", server_name="x3", group_id=2, group_path="Other"),
    ]
    return inv


def test_cms_targets_honours_group():
    inv = _inventory()
    assert len(cli_mod._cms_targets(inv, "DevGroup")) == 2
    assert len(cli_mod._cms_targets(inv, None)) == 5


def test_cms_targets_unknown_group_is_empty():
    assert cli_mod._cms_targets(_inventory(), "NoSuchGroup") == []


@pytest.mark.parametrize("group,expected", [("DevGroup", 2), (None, 5)])
def test_scoped_count_matches_what_will_be_audited(group, expected):
    """The announced count must equal the servers the run actually touches --
    the number in the log has to describe the audit the log is recording."""
    from sqldoc.cms import select_servers
    inv = _inventory()
    assert len(cli_mod._cms_targets(inv, group)) == len(select_servers(inv, group)) == expected
