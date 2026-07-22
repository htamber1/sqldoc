"""Tests for the `sqldoc doctor` environment-diagnostic command."""
from click.testing import CliRunner

from sqldoc import doctor
from sqldoc.doctor import OK, WARN, FAIL, Check, Report, run_checks
from sqldoc.cli import cli


def test_report_status_and_ok():
    r = Report([Check("a", OK), Check("b", WARN)])
    assert r.status == WARN and r.ok is True
    r2 = Report([Check("a", OK), Check("b", FAIL)])
    assert r2.status == FAIL and r2.ok is False


def test_check_python_ok():
    c = doctor.check_python()
    assert c.status == OK  # the test runner is a supported Python


def test_check_config_missing_is_ok():
    c = doctor.check_config("nope.yml", exists=lambda p: False)
    assert c.status == OK and "no nope.yml" in c.detail


def test_check_config_reports_driver_and_winauth():
    c = doctor.check_config(
        "x.yml", exists=lambda p: True,
        load=lambda p: {"driver": "ODBC Driver 17 for SQL Server", "windows_auth": True})
    assert c.status == OK
    assert "driver=" in c.detail and "windows_auth=on" in c.detail


def test_check_config_invalid_yaml_fails():
    def boom(p):
        raise ValueError("bad yaml")
    c = doctor.check_config("x.yml", exists=lambda p: True, load=boom)
    assert c.status == FAIL


def test_check_odbc_drivers_only_17_warns():
    # Simulate a host with only Driver 17 (the work-laptop scenario).
    orig = doctor._list_odbc_drivers
    doctor._list_odbc_drivers = lambda: ["ODBC Driver 17 for SQL Server"]
    try:
        c = doctor.check_odbc_drivers()
    finally:
        doctor._list_odbc_drivers = orig
    assert c.status == WARN
    assert "ODBC Driver 17" in c.detail
    assert "driver:" in c.hint  # points the user at the config fix


def test_check_odbc_drivers_18_ok():
    orig = doctor._list_odbc_drivers
    doctor._list_odbc_drivers = lambda: ["ODBC Driver 18 for SQL Server"]
    try:
        c = doctor.check_odbc_drivers()
    finally:
        doctor._list_odbc_drivers = orig
    assert c.status == OK


def test_check_connection_success():
    class FakeCur:
        def execute(self, q): pass
        def fetchone(self): return (1,)
    class FakeConn:
        def cursor(self): return FakeCur()
        def close(self): pass
    c = doctor.check_connection("conn", connect=lambda cs: FakeConn())
    assert c.status == OK


def test_check_connection_failure():
    def boom(cs):
        raise RuntimeError("no route to host")
    c = doctor.check_connection("conn", connect=boom)
    assert c.status == FAIL and "no route to host" in c.detail


def test_run_checks_no_connection():
    report = run_checks(config_path="nope.yml", ollama_probe=lambda: (_ for _ in ()).throw(Exception()))
    names = {c.name for c in report.checks}
    assert "sqldoc" in names and "Python" in names
    assert not any(c.name == "Connection" for c in report.checks)


def test_doctor_cli_runs():
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    # Exit 0 (healthy) or 1 (a FAIL) are both valid; a crash (2/exception) is not.
    assert result.exit_code in (0, 1)
    assert "environment diagnostics" in result.output


def test_doctor_cli_json_output(tmp_path):
    out = tmp_path / "doc.json"
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--json", str(out)])
    assert result.exit_code in (0, 1)
    import json
    data = json.loads(out.read_text())
    assert "checks" in data and "status" in data
