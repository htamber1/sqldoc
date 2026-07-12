"""Script output formats + direct execute."""
import pytest

from sqldoc.access import formats
from sqldoc.access.formats import (render_format, split_batches, execute_batches,
                                   to_powershell, to_ansible, extension_for, FORMATS)
from sqldoc.access.script import generate_script
from sqldoc.access.model import AccessReport, ADUser, Login, ParsedRequest


def _gs():
    r = AccessReport(user=ADUser(identifier="u", login="CORP\\jsmith", groups=["Sales Team"], found=True))
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    return generate_script(r, ParsedRequest(raw="write Sales", database="Sales", level="write"),
                           "prod", "Sales")


# --- batching --------------------------------------------------------------

def test_split_batches():
    sql = "USE [Sales];\nGO\nALTER ROLE [db_datareader] ADD MEMBER [x];\nGO\n"
    b = split_batches(sql)
    assert len(b) == 2 and b[0] == "USE [Sales];"


def test_execute_batches_runs_each():
    calls = []

    class Cur:
        def execute(self, sql):
            calls.append(sql)
    n = execute_batches(Cur(), "SELECT 1;\nGO\nSELECT 2;\nGO")
    assert n == 2 and len(calls) == 2


# --- formats ---------------------------------------------------------------

def test_all_formats_render():
    gs = _gs()
    for fmt in FORMATS:
        text = render_format(gs, fmt)
        assert text and "ALTER ROLE" in text or "mssql_script" in text or "Invoke-Sqlcmd" in text


def test_powershell_has_sqlcmd_and_rollback():
    text = to_powershell(_gs())
    assert "Invoke-Sqlcmd" in text and "-Rollback" in text
    assert "@'" in text and "'@" in text        # here-strings


def test_ansible_has_module_and_rollback():
    text = to_ansible(_gs())
    assert "community.general.mssql_script" in text
    assert "rollback | bool" in text


def test_runbook_uses_automation_credential():
    text = render_format(_gs(), "runbook")
    assert "Get-AutomationPSCredential" in text and "Invoke-Sqlcmd" in text


def test_sql_format_appends_commented_rollback():
    text = render_format(_gs(), "sql")
    assert "ROLLBACK" in text and "-- ALTER ROLE" in text


def test_extension_for():
    assert extension_for("powershell") == ".ps1" and extension_for("ansible") == ".yml"


def test_unknown_format():
    with pytest.raises(ValueError):
        render_format(_gs(), "cobol")


# --- CLI: access script --format -------------------------------------------

def test_cli_script_format_output(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.access import checker
    from sqldoc.access.model import AccessReport, ADUser, Login
    r = AccessReport(user=ADUser(identifier="jsmith", login="CORP\\jsmith", groups=["Sales Team"], found=True))
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    monkeypatch.setattr(checker, "check_access", lambda c, i, **k: r)
    monkeypatch.setattr(cli, "_access_tables_for", lambda cfg, db: ([], [], "prod", "sqlserver", None))
    cfg = {"access": {"ad": {"type": "native"},
                      "servers": [{"name": "prod", "connection_string": "c",
                                   "dialect": "sqlserver", "databases": ["Sales"]}]}}
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    ps = tmp_path / "grant.ps1"
    res = CliRunner().invoke(cli.cli, ["access", "script", "--config", str(p), "--user", "jsmith",
                                       "--database", "Sales", "--level", "write", "--no-ai",
                                       "--output", str(tmp_path / "s.html"),
                                       "--format", "powershell", "--out", str(ps)])
    assert res.exit_code == 0, res.output
    assert ps.exists() and "Invoke-Sqlcmd" in ps.read_text(encoding="utf-8")


# --- CLI: access execute ---------------------------------------------------

class _Cur:
    def __init__(self):
        self.run = []

    def execute(self, sql):
        self.run.append(sql)


class _Conn:
    def __init__(self, cur):
        self._c = cur

    def commit(self):
        pass

    def close(self):
        pass


class _Adapter:
    def __init__(self):
        self.cur = _Cur()

    def connect(self):
        return _Conn(self.cur)

    def cursor(self, conn):
        return self.cur


def test_cli_execute_runs_with_confirm(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.access import checker
    from sqldoc.access.model import AccessReport, ADUser, Login
    import sqldoc.audit as audit_mod
    r = AccessReport(user=ADUser(identifier="jsmith", login="CORP\\jsmith", groups=["Sales Team"], found=True))
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    adapter = _Adapter()
    logged = []
    monkeypatch.setattr(checker, "check_access", lambda c, i, **k: r)
    monkeypatch.setattr(cli, "_access_tables_for", lambda cfg, db: ([], [], "prod", "sqlserver", None))
    monkeypatch.setattr("sqldoc.access.checker.build_db_adapter", lambda entry, db: adapter)
    monkeypatch.setattr(audit_mod, "record", lambda **k: logged.append(k))
    cfg = {"access": {"ad": {"type": "native"},
                      "servers": [{"name": "prod", "connection_string": "c",
                                   "dialect": "sqlserver", "databases": ["Sales"]}]}}
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["access", "execute", "--config", str(p), "--user", "jsmith",
                                       "--database", "Sales", "--level", "write", "--no-ai", "--yes"])
    assert res.exit_code == 0, res.output
    assert adapter.cur.run          # batches executed
    assert "batch(es) executed" in res.output
    assert logged and logged[0]["command"] == "access.execute" and logged[0]["result"] == "executed"


def test_cli_execute_abort_without_confirm(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.access import checker
    from sqldoc.access.model import AccessReport, ADUser, Login
    r = AccessReport(user=ADUser(identifier="jsmith", login="CORP\\jsmith", groups=["Sales Team"], found=True))
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    adapter = _Adapter()
    monkeypatch.setattr(checker, "check_access", lambda c, i, **k: r)
    monkeypatch.setattr(cli, "_access_tables_for", lambda cfg, db: ([], [], "prod", "sqlserver", None))
    monkeypatch.setattr("sqldoc.access.checker.build_db_adapter", lambda entry, db: adapter)
    cfg = {"access": {"ad": {"type": "native"},
                      "servers": [{"name": "prod", "connection_string": "c",
                                   "dialect": "sqlserver", "databases": ["Sales"]}]}}
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["access", "execute", "--config", str(p), "--user", "jsmith",
                                       "--database", "Sales", "--level", "write", "--no-ai"],
                             input="n\n")
    assert res.exit_code == 0
    assert "Aborted" in res.output and not adapter.cur.run
