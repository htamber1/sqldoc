"""`sqldoc access check` must never report a false all-clear.

Regression cover for a field bug: with no `access:` config the identity source
auto-detects to `native`, which returns found=True with no groups WITHOUT
contacting anything, and zero servers are configured. The command nonetheless
printed "Resolving ... in Active Directory..." followed by a green "No SQL
Server access found." -- indistinguishable from a verified negative, for a user
who was in fact sysadmin.

"No access" is only a finding if something was actually checked.

Pure CLI-level tests; no database or directory required.
"""
import pytest
import yaml
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.access import ad as ad_mod, config as access_config
from sqldoc.access.model import ADUser, AccessReport, DatabaseAccess

CFG_WITH_SERVER = {"access": {"servers": [
    {"name": "s1", "connection_string": "DRIVER={x};SERVER=s1;", "databases": ["D1"]}]}}


def combined(result):
    """stdout + stderr. Click >= 8.2 keeps the streams separate and the warnings
    under test are written to stderr."""
    out = result.output or ""
    try:
        err = result.stderr or ""
    except (ValueError, AttributeError):   # older Click already mixed them in
        err = ""
    return out if err and err in out else out + err


def run_check(tmp_path, monkeypatch, cfg_dict, report=None):
    """Invoke `access check` against a temp config; returns the click Result."""
    cfg_path = tmp_path / "cfg.yml"
    cfg_path.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
    if report is not None:
        import sqldoc.access.checker as checker_mod
        monkeypatch.setattr(checker_mod, "check_access",
                            lambda cfg, ident, **kw: report)
    return CliRunner().invoke(
        cli.access_check,
        ["--config", str(cfg_path), "--user", "someone",
         "--output", str(tmp_path / "out.html")])


@pytest.fixture
def empty_report():
    return AccessReport(user=ADUser(identifier="someone", display_name="Someone",
                                    source="native", found=True))


# --- the trap ---------------------------------------------------------------

def test_empty_config_auto_detects_the_no_op_native_source():
    src = ad_mod.get_source({})
    assert src.source == "native"


def test_native_source_reports_found_having_contacted_nothing():
    user = ad_mod.get_source({}).get_user("someone")
    assert user.found is True
    assert user.groups == []


def test_no_servers_are_configured_by_default():
    assert access_config.servers({}) == []


# --- no config: warn, and do NOT read as an all-clear -----------------------

def test_empty_config_run_succeeds(tmp_path, monkeypatch):
    assert run_check(tmp_path, monkeypatch, {}).exit_code == 0


def test_names_the_real_source_not_active_directory(tmp_path, monkeypatch):
    out = combined(run_check(tmp_path, monkeypatch, {}))
    assert "via native" in out
    assert "in Active Directory" not in out


def test_warns_that_no_directory_was_queried(tmp_path, monkeypatch):
    out = combined(run_check(tmp_path, monkeypatch, {}))
    assert "No 'access.ad:' section configured" in out


def test_warns_that_no_sql_server_was_contacted(tmp_path, monkeypatch):
    out = combined(run_check(tmp_path, monkeypatch, {}))
    assert "No 'access.servers:' configured" in out


def test_states_the_result_is_not_a_confirmation(tmp_path, monkeypatch):
    out = combined(run_check(tmp_path, monkeypatch, {}))
    assert "NOT a confirmation" in out
    assert "No SQL Server access found" not in out


# --- servers configured + genuinely no access: the all-clear IS correct -----

def test_all_clear_is_printed_when_servers_were_checked(tmp_path, monkeypatch,
                                                        empty_report):
    out = combined(run_check(tmp_path, monkeypatch, CFG_WITH_SERVER, empty_report))
    assert "No SQL Server access found" in out
    assert "checked 1 server(s)" in out


def test_no_false_warnings_when_servers_were_checked(tmp_path, monkeypatch,
                                                     empty_report):
    out = combined(run_check(tmp_path, monkeypatch, CFG_WITH_SERVER, empty_report))
    assert "NOT a confirmation" not in out
    assert "No 'access.servers:' configured" not in out


# --- real access found ------------------------------------------------------

def test_access_rows_are_reported_without_an_all_clear(tmp_path, monkeypatch):
    report = AccessReport(user=ADUser(identifier="someone", source="native",
                                      found=True))
    report.access.append(DatabaseAccess(server="s1", database="D1", login="X",
                                        level="read", roles=["db_datareader"]))
    out = combined(run_check(tmp_path, monkeypatch, CFG_WITH_SERVER, report))
    assert "s1/D1" in out
    assert "No SQL Server access found" not in out


# --- a configured directory is named as itself ------------------------------

def test_a_configured_directory_is_named_as_itself(tmp_path, monkeypatch,
                                                   empty_report):
    cfg = {"access": {"ad": {"type": "ldap", "server": "ldap://dc.example.test",
                             "base_dn": "DC=example,DC=test"}}}
    out = combined(run_check(tmp_path, monkeypatch, cfg, empty_report))
    assert "via ldap" in out
    assert "No 'access.ad:' section configured" not in out
