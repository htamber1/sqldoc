"""Every SQL Server connection-string path honours the `driver:` override, and
every dialect builds its own native connection string.

Regression cover for three classes of field bug found against real infrastructure:

  A. `driver:` was ignored at 9+ call sites (CMS, the access suite, multi-tenant,
     the agent), so a host with only ODBC Driver 17 silently failed even though
     the config named it -- and `sqldoc doctor` recommended the exact fix that
     those paths ignored.
  B. postgres/mysql/sqlite/mongodb/snowflake entries configured with discrete
     server/username/password parts were handed a SQL Server ODBC string.
  C. `.sqldoc.yml` was read only from the current directory, so running from a
     subdirectory silently dropped every setting, surfacing as a bare ODBC IM002.

Offline: builds connection strings only, never connects.
"""
import os
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.adapters import (DIALECTS, SqlServerAdapter, UnsupportedDialectError,
                             build_connection_string_for)
from sqldoc.extractor import build_connection_string

D17 = "ODBC Driver 17 for SQL Server"
D18 = "ODBC Driver 18 for SQL Server"
CFG = {"driver": D17}

# Every dialect whose adapter derives from SqlServerAdapter, so the ODBC
# `driver:` override must reach it.
SQLSERVER_FAMILY = ("sqlserver", "azuresql", "azure_managed_instance", "synapse")

# Dialects that build a URI/path instead: they must accept the driver argument
# without raising and without leaking an ODBC driver into the string.
OTHER_DIALECTS = ("postgres", "mysql", "sqlite", "mongodb", "snowflake", "oracle",
                  "redshift", "cockroachdb", "aurora_postgres", "aurora_mysql",
                  "db2", "bigquery", "databricks")


def has_driver(conn_str, driver=D17):
    return f"DRIVER={{{driver}}}" in str(conn_str)


# --- A. the canonical builders ---------------------------------------------

def test_extractor_build_connection_string_honours_driver():
    assert has_driver(build_connection_string("s", "db", "u", "p", driver=D17))


def test_adapter_build_connection_string_honours_driver():
    assert has_driver(
        SqlServerAdapter.build_connection_string("s", "db", "u", "p", driver=D17))


def test_defaults_to_driver_18_when_unset():
    assert has_driver(build_connection_string("s", "db", "u", "p"), driver=D18)
    assert SqlServerAdapter.DEFAULT_DRIVER == D18


# --- A. the CMS paths -------------------------------------------------------

def test_cms_connection_string_for_honours_driver():
    from sqldoc import cms
    assert has_driver(cms.connection_string_for("s", driver=D17))


def test_cms_connection_string_for_defaults_to_18():
    from sqldoc import cms
    assert has_driver(cms.connection_string_for("s"), driver=D18)


def test_cms_bulk_adapter_honours_driver():
    from sqldoc.cms import CmsServer
    from sqldoc.cms_bulk import _adapter_for
    adapter = _adapter_for(CmsServer(name="s", server_name="s"),
                           {"driver": D17, "windows_auth": True})
    assert has_driver(adapter.connection_string)


def test_agent_cms_monitor_build_databases_honours_driver():
    from sqldoc.agent import cms_monitor
    from sqldoc.cms import CmsInventory, CmsServer
    inv = CmsInventory(servers=[CmsServer(name="s", server_name="s")])
    dbs = cms_monitor.build_databases(inv, {"server": "cms", "driver": D17})
    assert has_driver(dbs[0].connection_string)


# --- A. the paths this patch newly audited ----------------------------------

def test_access_config_servers_carries_driver():
    from sqldoc.access import config as access_config
    entries = access_config.servers({
        "driver": D17,
        "access": {"servers": [{"name": "p", "server": "s", "username": "u",
                                "password": "p", "databases": ["Sales"]}]}})
    assert entries[0]["driver"] == D17


def test_access_checker_build_db_adapter_honours_driver():
    from sqldoc.access import config as access_config
    from sqldoc.access.checker import build_db_adapter
    entries = access_config.servers({
        "driver": D17,
        "access": {"servers": [{"name": "p", "server": "s", "username": "u",
                                "password": "p", "databases": ["Sales"]}]}})
    assert has_driver(build_db_adapter(entries[0], "Sales").connection_string)


def test_cli_adapter_from_db_entry_honours_driver():
    _name, _adapter, conn_str = cli._adapter_from_db_entry(
        {"name": "d", "server": "s", "database": "db",
         "username": "u", "password": "p"}, CFG)
    assert has_driver(conn_str)


def test_cli_db_entry_driver_beats_top_level():
    _n, _a, conn_str = cli._adapter_from_db_entry(
        {"name": "d", "server": "s", "database": "db", "username": "u",
         "password": "p", "driver": "ODBC Driver 13 for SQL Server"}, CFG)
    assert has_driver(conn_str, "ODBC Driver 13 for SQL Server")


def test_cli_load_tenants_honours_driver():
    reg = cli.load_tenants({"driver": D17, "tenants": [
        {"name": "t", "api_key": "k", "server": "s", "database": "db",
         "username": "u", "password": "p"}]})
    assert has_driver(list(reg.values())[0]["conn_str"])


def test_agent_resolve_connection_honours_driver():
    from sqldoc.agent.config import _resolve_connection
    conn_str, _dialect = _resolve_connection(
        {"name": "d", "server": "s", "database": "db",
         "username": "u", "password": "p"}, D17)
    assert has_driver(conn_str)


def test_agent_cms_inherits_top_level_driver():
    """Without this inheritance the cms_monitor fix above is inert."""
    from sqldoc.agent.config import parse_agent_config
    ac = parse_agent_config({"driver": D17, "agent": {
        "cms": {"server": "cms"},
        "databases": [{"name": "d", "server": "s", "database": "db",
                       "username": "u", "password": "p"}]}})
    assert (ac.cms or {}).get("driver") == D17


def test_agent_explicit_cms_driver_is_not_overwritten():
    from sqldoc.agent.config import parse_agent_config
    ac = parse_agent_config({"driver": D17, "agent": {
        "cms": {"server": "cms", "driver": D18},
        "databases": []}})
    assert (ac.cms or {}).get("driver") == D18


def test_agent_resolve_connection_leaves_postgres_alone():
    """The driver override must not be passed to an adapter that takes no
    driver argument -- that would be a TypeError, not a degraded string."""
    from sqldoc.agent.config import _resolve_connection
    conn_str, dialect = _resolve_connection(
        {"name": "p", "dialect": "postgres", "server": "h", "database": "db",
         "username": "u", "password": "p"}, D17)
    assert dialect == "postgres"
    assert conn_str.startswith("postgresql://")


# --- B. dialect-aware connection strings ------------------------------------

@pytest.mark.parametrize("name", sorted(n for n, c in DIALECTS.items() if c is not None))
def test_every_dialect_builds_its_own_adapters_native_form(name):
    cls = DIALECTS[name]
    got = build_connection_string_for(name, "h", "db", "u", "p")
    if issubclass(cls, SqlServerAdapter):
        want = cls.build_connection_string("h", "db", "u", "p", windows_auth=False)
    else:
        want = cls.build_connection_string("h", "db", "u", "p")
    assert got == want


@pytest.mark.parametrize("name", SQLSERVER_FAMILY)
def test_driver_override_reaches_the_whole_sqlserver_family(name):
    assert has_driver(build_connection_string_for(name, "h", "db", "u", "p", driver=D17))


@pytest.mark.parametrize("name", OTHER_DIALECTS)
def test_driver_is_accepted_and_ignored_by_other_dialects(name):
    conn_str = build_connection_string_for(name, "h", "db", "u", "p", driver=D17)
    assert "ODBC" not in conn_str


def test_windows_auth_reaches_the_sqlserver_family():
    conn_str = build_connection_string_for("sqlserver", "h", "db", None, None,
                                           windows_auth=True)
    assert "Trusted_Connection=yes" in conn_str
    assert "UID=" not in conn_str


def test_unknown_dialect_is_rejected():
    with pytest.raises(UnsupportedDialectError):
        build_connection_string_for("oracel", "h", "db", "u", "p")


def test_db_entry_with_non_sqlserver_dialect_builds_native_string():
    _n, _a, conn_str = cli._adapter_from_db_entry(
        {"name": "d", "dialect": "postgres", "server": "h", "database": "db",
         "username": "u", "password": "p"}, CFG)
    assert conn_str == "postgresql://u:p@h/db"


def test_tenant_with_non_sqlserver_dialect_builds_native_string():
    reg = cli.load_tenants({"driver": D17, "tenants": [
        {"name": "t", "api_key": "k2", "dialect": "mysql", "server": "h",
         "database": "db", "username": "u", "password": "p"}]})
    assert list(reg.values())[0]["conn_str"] == "mysql://u:p@h/db"


# --- C. config discovery ----------------------------------------------------

def _ancestor_has_config(directory):
    """True if any ancestor of `directory` holds a .sqldoc.yml.

    pytest's tmp_path lives under the user profile, which could itself carry a
    config; the miss-case assertions below are only meaningful without one.
    """
    current = os.path.abspath(directory)
    while True:
        if os.path.exists(os.path.join(current, ".sqldoc.yml")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


@pytest.fixture
def config_tree(tmp_path, monkeypatch):
    """A project with .sqldoc.yml at its root and a deep subdirectory."""
    if _ancestor_has_config(tmp_path):
        pytest.skip("an ancestor of tmp_path holds a .sqldoc.yml")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    cfg = tmp_path / ".sqldoc.yml"
    cfg.write_text(f"driver: '{D17}'\n", encoding="utf-8")
    monkeypatch.chdir(deep)
    return tmp_path, deep, cfg


def test_find_config_searches_parent_directories(config_tree):
    root, _deep, cfg = config_tree
    assert cli.find_config(".sqldoc.yml", False) == str(cfg)


def test_find_config_prefers_the_file_in_cwd(config_tree, monkeypatch):
    root, _deep, _cfg = config_tree
    monkeypatch.chdir(root)
    assert cli.find_config(".sqldoc.yml", False) == ".sqldoc.yml"


def test_explicit_config_wins_over_the_search(config_tree):
    assert cli.find_config(".sqldoc.yml", True) == ".sqldoc.yml"


def test_path_with_a_directory_component_is_untouched(config_tree):
    assert cli.find_config("cfg/other.yml", False) == "cfg/other.yml"


def test_find_config_falls_back_to_the_bare_default(tmp_path, monkeypatch):
    """With no config in any ancestor the search must terminate at the
    filesystem root and hand back the default name."""
    if _ancestor_has_config(tmp_path):
        pytest.skip("an ancestor of tmp_path holds a .sqldoc.yml")
    monkeypatch.chdir(tmp_path)
    assert cli.find_config(".sqldoc.yml", False) == ".sqldoc.yml"


def test_load_config_reads_the_parent_config(config_tree):
    assert cli.load_config(".sqldoc.yml", False).get("driver") == D17


def test_load_config_announces_the_parent_config(config_tree, capsys):
    """A parent config is never adopted silently."""
    cli.load_config(".sqldoc.yml", False)
    captured = capsys.readouterr()
    assert "Using config:" in (captured.err + captured.out)


# --- C. the IM002 hint ------------------------------------------------------

def test_im002_hint_names_the_config_in_effect(config_tree):
    _root, _deep, cfg = config_tree
    hint = cli._driver_error_hint(
        Exception("('IM002', '[IM002] ... no default driver specified')"))
    assert str(cfg) in hint
    assert "driver:" in hint
    assert SqlServerAdapter.DEFAULT_DRIVER in hint


def test_im002_hint_reports_when_no_config_was_found(tmp_path, monkeypatch):
    if _ancestor_has_config(tmp_path):
        pytest.skip("an ancestor of tmp_path holds a .sqldoc.yml")
    monkeypatch.chdir(tmp_path)
    hint = cli._driver_error_hint(Exception("('IM002', 'Data source name not found')"))
    assert "or any parent directory" in hint


def test_unrelated_errors_get_no_driver_hint():
    assert cli._driver_error_hint(Exception("Login failed for user 'sa'")) == ""


@pytest.mark.parametrize("message", [
    "('IM002', '[IM002] [Microsoft][ODBC Driver Manager] Data source name not "
    "found and no default driver specified')",
    "Data source name not found",
])
def test_driver_hint_fires_for_both_im002_spellings(message, config_tree):
    assert cli._driver_error_hint(Exception(message)) != ""


# --- C. the hint reaches every connecting command ---------------------------

IM002 = ("('IM002', '[IM002] [Microsoft][ODBC Driver Manager] Data source name "
         "not found and no default driver specified (0) (SQLDriverConnect)')")


def test_abort_connection_failed_reports_and_hints(config_tree, capsys):
    with pytest.raises(click.Abort):
        cli._abort_connection_failed(Exception(IM002))
    err = capsys.readouterr().err
    assert "Connection failed:" in err
    assert "driver:" in err
    assert "sqldoc doctor" in err


def test_abort_connection_failed_stays_quiet_for_other_failures(config_tree, capsys):
    """A bad password must not be blamed on the ODBC driver."""
    with pytest.raises(click.Abort):
        cli._abort_connection_failed(Exception("Login failed for user 'sa'"))
    err = capsys.readouterr().err
    assert "Connection failed:" in err
    assert "sqldoc doctor" not in err


# --- C2. a query error must not be reported as a connection failure ---------
#
# The connecting commands wrap extraction, not just connect(), so a statement
# the server parses and refuses arrives at the same handler as a dead host.
# v3.0.4 reported every one of them as "Connection failed", which is what made
# the SQL Server 2016 STRING_AGG failure so slow to diagnose: the connection was
# fine, and the message sent people to check firewalls and passwords.

MSG195 = ("('42000', \"[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
          "'STRING_AGG' is not a recognized built-in function name. (195) "
          "(SQLExecDirectW)\")")

PERMISSION_DENIED = ("('42000', \"[42000] [Microsoft][ODBC Driver 18 for SQL Server]"
                     "[SQL Server]The SELECT permission was denied on the object "
                     "'objects', database 'mssqlsystemresource', schema 'sys'. (229)\")")

COMM_LINK_FAILURE = ("('08S01', '[08S01] [Microsoft][ODBC Driver 18 for SQL Server]"
                     "TCP Provider: An existing connection was forcibly closed (10054)')")

LOGIN_FAILED = ("('28000', \"[28000] [Microsoft][ODBC Driver 18 for SQL Server]"
                "[SQL Server]Login failed for user 'sa'. (18456)\")")


def test_unrecognized_builtin_is_reported_as_a_query_error(config_tree, capsys):
    """The headline case: Msg 195 on a pre-2017 server."""
    with pytest.raises(click.Abort):
        cli._abort_connection_failed(Exception(MSG195))
    err = capsys.readouterr().err
    assert "Query failed:" in err
    assert "Connection failed:" not in err, (
        "A Msg 195 was blamed on the connection; the connection succeeded.")


def test_unrecognized_builtin_names_the_version_cause(config_tree, capsys):
    with pytest.raises(click.Abort):
        cli._abort_connection_failed(Exception(MSG195))
    err = capsys.readouterr().err
    assert "STRING_AGG" in err
    assert "SQL Server 2017" in err, "the hint must name the version that introduced it"
    assert "2016" in err, "the hint must state the supported floor"


def test_unknown_builtin_outside_the_table_still_explains_itself(config_tree, capsys):
    """A function sqldoc has no floor recorded for must still be reported as a
    query error rather than silently falling back to 'Connection failed'."""
    exc = Exception("('42000', \"[42000] 'SOME_FUTURE_FN' is not a recognized "
                    "built-in function name. (195)\")")
    with pytest.raises(click.Abort):
        cli._abort_connection_failed(exc)
    err = capsys.readouterr().err
    assert "Query failed:" in err
    assert "SOME_FUTURE_FN" in err


def test_permission_denied_is_a_query_error_not_a_connection_failure(config_tree, capsys):
    with pytest.raises(click.Abort):
        cli._abort_connection_failed(Exception(PERMISSION_DENIED))
    err = capsys.readouterr().err
    assert "Query failed:" in err
    assert "permission" in err.lower()


@pytest.mark.parametrize("message", [COMM_LINK_FAILURE, LOGIN_FAILED])
def test_genuine_connection_failures_keep_their_wording(config_tree, capsys, message):
    """08 (connection) and 28 (authorization) must not be reclassified."""
    with pytest.raises(click.Abort):
        cli._abort_connection_failed(Exception(message))
    err = capsys.readouterr().err
    assert "Connection failed:" in err
    assert "Query failed:" not in err


def test_ambiguous_failure_keeps_the_connection_wording(config_tree, capsys):
    """No SQLSTATE and no recognizable server text: do not guess."""
    with pytest.raises(click.Abort):
        cli._abort_connection_failed(Exception("something went wrong"))
    err = capsys.readouterr().err
    assert "Connection failed:" in err


@pytest.mark.parametrize("exc,expected", [
    (Exception(MSG195), "42000"),
    (Exception(COMM_LINK_FAILURE), "08S01"),
    (Exception(IM002), "IM002"),
    (Exception("no sqlstate here"), ""),
])
def test_sqlstate_is_recovered_from_stringified_errors(exc, expected):
    """Errors are usually stringified by the time they reach the handler, so the
    SQLSTATE has to be readable out of the message, not just args[0]."""
    assert cli._sqlstate(exc) == expected


def test_sqlstate_prefers_args_when_the_exception_carries_it():
    """A live pyodbc error puts the SQLSTATE in args[0]."""
    exc = Exception("42000", "[42000] ...")
    assert cli._sqlstate(exc) == "42000"


def test_no_command_still_uses_the_bare_connection_failure_message():
    """Structural guard: every connecting command must route through
    `_abort_connection_failed` so none of them can regress to a bare IM002."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert 'click.echo(f"Connection failed: {e}", err=True)' not in source
    assert source.count("_abort_connection_failed(e)") >= 17


@pytest.mark.parametrize("command,extra", [
    ("doc", ["--no-ai"]),
    ("scan", []),
])
def test_connecting_commands_emit_the_hint_end_to_end(command, extra, tmp_path,
                                                      monkeypatch, config_tree):
    """The failure this eliminates: `sqldoc doc` against a Driver-17-only host
    printed a bare IM002 that never mentioned the driver or the config file."""
    def boom(*a, **kw):
        raise Exception(IM002)

    monkeypatch.setattr(cli, "extract_metadata", boom)
    result = CliRunner().invoke(cli.cli, [
        command, "--server", "s", "--database", "db",
        "--username", "u", "--password", "p",
        "--output", str(tmp_path / "out.html")] + extra)
    combined = (result.output or "")
    try:
        combined += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    assert result.exit_code != 0
    assert "Connection failed:" in combined
    assert "driver:" in combined
    assert "sqldoc doctor" in combined


def test_integration_push_path_hints_too(config_tree, capsys):
    """`_run_integration` builds its own connection for the --push commands."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    marker = source.index("def _run_integration")
    body = source[marker:marker + 4000]
    assert "_driver_error_hint" in body


def test_multi_database_comply_hints_once_not_per_database():
    """A driver mismatch fails every entry for the same reason; the hint is
    collected in the loop and printed once after it."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "driver_hint = driver_hint or _driver_error_hint(e)" in source
