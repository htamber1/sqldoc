"""`access.servers` entries support `windows_auth:` (and `driver:`).

Before this, an `access.servers` entry accepted only a `connection_string` or
server+username+password, so a Windows-auth shop had to hand-write a
Trusted_Connection ODBC string -- while every other part of sqldoc honours
`windows_auth: true`. Both settings are now inherited from the top level.

Inheritance is tested against None rather than falsiness, so an explicit
per-entry `windows_auth: false` still overrides a global true.

Pure functions; no database or directory required.
"""
import pytest

from sqldoc.access import config as access_config
from sqldoc.access.checker import build_db_adapter

D17 = "ODBC Driver 17 for SQL Server"


def entry(server_entry, **top_level):
    cfg = {"access": {"servers": [server_entry]}}
    cfg.update(top_level)
    return access_config.servers(cfg)[0]


# --- parsing + inheritance --------------------------------------------------

def test_per_entry_windows_auth_is_parsed():
    assert entry({"name": "s", "server": "sql1", "windows_auth": True})["windows_auth"] is True


def test_absent_windows_auth_defaults_to_false():
    assert entry({"name": "s", "server": "sql1"})["windows_auth"] is False


def test_inherits_top_level_windows_auth():
    assert entry({"name": "s", "server": "sql1"}, windows_auth=True)["windows_auth"] is True


def test_explicit_per_entry_false_overrides_top_level_true():
    got = entry({"name": "s", "server": "sql1", "windows_auth": False},
                windows_auth=True)
    assert got["windows_auth"] is False


def test_explicit_per_entry_true_with_top_level_false():
    got = entry({"name": "s", "server": "sql1", "windows_auth": True},
                windows_auth=False)
    assert got["windows_auth"] is True


# --- the connection string actually built -----------------------------------

@pytest.fixture
def windows_auth_conn_str():
    e = entry({"name": "s", "server": "sql1", "windows_auth": True}, driver=D17)
    return build_db_adapter(e, "AppDB").connection_string


def test_windows_auth_uses_trusted_connection(windows_auth_conn_str):
    assert "Trusted_Connection=yes" in windows_auth_conn_str


def test_windows_auth_carries_the_configured_driver(windows_auth_conn_str):
    assert f"DRIVER={{{D17}}}" in windows_auth_conn_str


def test_windows_auth_targets_the_right_server_and_database(windows_auth_conn_str):
    assert "SERVER=sql1;" in windows_auth_conn_str
    assert "DATABASE=AppDB;" in windows_auth_conn_str


def test_windows_auth_sends_no_credentials(windows_auth_conn_str):
    assert "UID=" not in windows_auth_conn_str
    assert "PWD=" not in windows_auth_conn_str


def test_windows_auth_needs_no_credentials_configured():
    """The original blocker: an entry with windows_auth and no username/password
    must build rather than fail the missing-settings check."""
    e = entry({"name": "s", "server": "sql1", "windows_auth": True})
    assert "Trusted_Connection=yes" in build_db_adapter(e, "AppDB").connection_string


# --- SQL auth is unchanged --------------------------------------------------

def test_sql_auth_still_builds_uid_pwd():
    e = entry({"name": "s", "server": "sql1", "username": "sa", "password": "p"},
              driver=D17)
    conn_str = build_db_adapter(e, "AppDB").connection_string
    assert "UID=sa;" in conn_str
    assert "PWD={p};" in conn_str
    assert "Trusted_Connection" not in conn_str


# --- an explicit connection_string still wins -------------------------------

def test_explicit_connection_string_is_used_verbatim():
    e = entry({"name": "s", "connection_string": "postgresql://u:p@pg1/analytics",
               "dialect": "postgres"})
    assert build_db_adapter(e, "analytics").connection_string.startswith("postgresql://")


# --- windows_auth must not leak into other dialects -------------------------

def test_windows_auth_does_not_leak_into_postgres():
    e = entry({"name": "s", "server": "pg1", "dialect": "postgres",
               "username": "u", "password": "p", "windows_auth": True})
    assert build_db_adapter(e, "analytics").connection_string == "postgresql://u:p@pg1/analytics"
