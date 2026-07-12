"""SQL Server login-type classification + correct CREATE LOGIN/USER syntax."""
import pytest

from sqldoc.access import login_types as lt
from sqldoc.access.script import generate_script
from sqldoc.access.model import AccessReport, ADUser, Login, ParsedRequest


# --- classification --------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("CORP\\Sales Team", lt.WINDOWS),
    ("CORP\\jsmith", lt.WINDOWS),
    ("app_login", lt.SQL),
    ("jsmith@corp.com", lt.AZURE_AD),
])
def test_classify_by_name(name, expected):
    assert lt.classify_login(name) == expected


def test_classify_hint_overrides():
    assert lt.classify_login("app_login", hint="managed_identity") == lt.MANAGED_IDENTITY
    assert lt.classify_login("jsmith@corp.com", hint="sql") == lt.SQL
    assert lt.classify_login("x", hint="aad") == lt.AZURE_AD


# --- DDL per type ----------------------------------------------------------

def test_create_login_windows():
    assert lt.create_login_sql("CORP\\Grp", lt.WINDOWS) == "CREATE LOGIN [CORP\\Grp] FROM WINDOWS;"


def test_create_login_external():
    assert "FROM EXTERNAL PROVIDER" in lt.create_login_sql("u@corp.com", lt.AZURE_AD)
    assert "FROM EXTERNAL PROVIDER" in lt.create_login_sql("mi-app", lt.MANAGED_IDENTITY)


def test_create_login_sql_native():
    assert "WITH PASSWORD" in lt.create_login_sql("app", lt.SQL)


def test_azure_sql_db_contained_user():
    # Azure SQL Database: external principals are contained users, no server login.
    assert lt.needs_server_login(lt.AZURE_AD, "azuresql") is False
    assert lt.create_user_sql("u@corp.com", lt.AZURE_AD, "azuresql") == \
        "CREATE USER [u@corp.com] FROM EXTERNAL PROVIDER;"
    # Managed Instance keeps a server login.
    assert lt.needs_server_login(lt.AZURE_AD, "azure_managed_instance") is True


# --- end-to-end via generate_script ----------------------------------------

def _report(user_login, groups=()):
    return AccessReport(user=ADUser(identifier="u", login=user_login, groups=list(groups), found=True))


def test_generate_azure_ad_login():
    r = _report("jsmith@corp.com")
    parsed = ParsedRequest(raw="read Sales", database="Sales", level="read")
    gs = generate_script(r, parsed, "prod", "Sales", login_override="jsmith@corp.com")
    assert gs.login_type == lt.AZURE_AD
    assert "CREATE LOGIN [jsmith@corp.com] FROM EXTERNAL PROVIDER;" in gs.grant_sql
    assert "CREATE USER [jsmith@corp.com] FOR LOGIN [jsmith@corp.com];" in gs.grant_sql


def test_generate_azure_sql_db_contained():
    r = _report("jsmith@corp.com")
    parsed = ParsedRequest(raw="read Sales", database="Sales", level="read")
    gs = generate_script(r, parsed, "azuredb", "Sales", login_override="jsmith@corp.com",
                         dialect="azuresql")
    assert "server login" in gs.grant_sql.lower() and "contained user" in gs.grant_sql.lower()
    assert "CREATE USER [jsmith@corp.com] FROM EXTERNAL PROVIDER;" in gs.grant_sql
    assert "USE [master]" not in gs.grant_sql       # no server-login step
    assert "DROP LOGIN" not in gs.rollback_sql


def test_generate_managed_identity():
    r = _report("app-mi")
    parsed = ParsedRequest(raw="read Sales", database="Sales", level="read")
    gs = generate_script(r, parsed, "prod", "Sales", login_override="app-mi",
                         login_type="managed_identity")
    assert gs.login_type == lt.MANAGED_IDENTITY
    assert "FROM EXTERNAL PROVIDER" in gs.grant_sql


def test_generate_sql_native_still_password():
    r = _report("appuser")
    parsed = ParsedRequest(raw="read Sales", database="Sales", level="read")
    gs = generate_script(r, parsed, "prod", "Sales", login_override="appuser")
    assert gs.login_type == lt.SQL and "WITH PASSWORD" in gs.grant_sql


def test_generate_windows_group_unchanged():
    r = _report("CORP\\jsmith", groups=["Sales Team"])
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    parsed = ParsedRequest(raw="read Sales", database="Sales", level="read")
    gs = generate_script(r, parsed, "prod", "Sales")
    assert gs.login_type == lt.WINDOWS and gs.uses_windows_group
    assert "FROM WINDOWS" in gs.grant_sql


def test_script_json_has_login_type():
    from sqldoc.access.render import build_script_json
    r = _report("jsmith@corp.com")
    gs = generate_script(r, ParsedRequest(raw="read Sales", database="Sales", level="read"),
                         "prod", "Sales", login_override="jsmith@corp.com")
    assert build_script_json(gs)["login_type"] == lt.AZURE_AD
