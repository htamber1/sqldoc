"""Security vulnerability scanner across dialects, scoring, render, CLI."""
import json

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.secure import collect_security, summarize, SecurityReport, SecurityFinding
from sqldoc.secure_renderer import build_secure_json, render_secure_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


# --- scoring ----------------------------------------------------------------

def test_score_deductions():
    r = SecurityReport(dialect="sqlserver", findings=[
        SecurityFinding("HIGH", "c", "t"), SecurityFinding("MEDIUM", "c", "t"),
        SecurityFinding("LOW", "c", "t")])
    assert r.score == 100 - 15 - 7 - 3          # 75
    assert r.grade == "B"
    assert SecurityReport(dialect="x").score == 100 and SecurityReport(dialect="x").grade == "A"


# --- SQL Server -------------------------------------------------------------

def test_sqlserver_checks(fake_mssql_secure_rows):
    report = collect_security(FakeAdapter(FakeConnection(fake_mssql_secure_rows), dialect="sqlserver"))
    titles = [f.title for f in report.findings]
    assert any("SA account is enabled" in t for t in titles)
    assert any("blank password" in t for t in titles)
    assert any("xp_cmdshell" in t for t in titles)
    assert any("TRUSTWORTHY" in t for t in titles)
    assert any("public role" in t for t in titles)
    s = summarize(report)
    assert s["high"] == 3 and s["medium"] == 2          # blank-pw, xp_cmdshell, trustworthy / sa, public
    assert s["score"] == 100 - 3 * 15 - 2 * 7           # 41
    assert s["grade"] == "D"
    # HIGH findings sort first
    assert report.findings[0].severity == "HIGH"


# --- PostgreSQL -------------------------------------------------------------

def test_postgres_checks(fake_pg_secure_rows):
    report = collect_security(FakeAdapter(FakeConnection(fake_pg_secure_rows), dialect="postgres"))
    titles = [f.title for f in report.findings]
    assert any("extra login-capable superuser" in t for t in titles)
    assert any("trust" in t for t in titles)
    assert any("plaintext 'password'" in t for t in titles)
    assert any("public schema" in t for t in titles)
    assert any("SSL/TLS is disabled" in t for t in titles)
    s = summarize(report)
    assert s["high"] == 1 and s["medium"] == 4 and s["low"] == 1


# --- MySQL ------------------------------------------------------------------

def test_mysql_checks(fake_mysql_secure_rows):
    report = collect_security(FakeAdapter(FakeConnection(fake_mysql_secure_rows), dialect="mysql"))
    titles = [f.title for f in report.findings]
    assert any("Anonymous account" in t for t in titles)
    assert any("root can log in remotely" in t for t in titles)
    assert any("no password" in t for t in titles)
    assert any("FILE privilege" in t for t in titles)
    assert any("secure_file_priv" in t for t in titles)
    s = summarize(report)
    assert s["high"] == 3 and s["medium"] == 2


def test_unsupported_dialect():
    report = collect_security(FakeAdapter(FakeConnection({}), dialect="sqlite"))
    assert not report.supported and report.score == 100


# --- render + json + CLI ----------------------------------------------------

def test_build_and_render(fake_mssql_secure_rows, tmp_path):
    report = collect_security(FakeAdapter(FakeConnection(fake_mssql_secure_rows), dialect="sqlserver"))
    data = build_secure_json("PRODSQL01", report)
    assert data["report_type"] == "security" and data["summary"]["score"] == 41
    assert any(f["severity"] == "HIGH" for f in data["findings"])

    out = tmp_path / "sec.html"
    render_secure_html("PRODSQL01", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Security score" in h and "GRADE D" in h and "xp_cmdshell" in h


def test_secure_cli(monkeypatch, fake_mssql_secure_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_mssql_secure_rows)))
    out = tmp_path / "sec.html"
    jout = tmp_path / "sec.json"
    res = CliRunner().invoke(cli.cli, [
        "secure", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Security score: 41/100" in res.output and "HIGH: 3" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["dialect"] == "sqlserver"


def test_secure_cli_fail_under(monkeypatch, fake_mssql_secure_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_mssql_secure_rows)))
    res = CliRunner().invoke(cli.cli, [
        "secure", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(tmp_path / "s.html"), "--fail-under", "80",
    ])
    assert res.exit_code == 1                     # score 41 < 80
    assert "below the --fail-under threshold" in res.output
