"""Pre-commit hook installer + SQL-file PII scanning (`scan-files` / `install-hooks`)."""
import os

from click.testing import CliRunner

from sqldoc import cli, hooks
from sqldoc.pii import columns_from_sql, scan_sql_files


# --- DDL parsing ------------------------------------------------------------

def test_columns_from_create_table():
    sql = """
    CREATE TABLE dbo.Customers (
        CustomerID   INT PRIMARY KEY,
        FirstName    NVARCHAR(50),
        SSN          CHAR(11),
        Balance      DECIMAL(10, 2),
        CONSTRAINT FK_x FOREIGN KEY (CustomerID) REFERENCES Other(Id)
    );
    """
    cols = columns_from_sql(sql)
    names = {c for _, c in cols}
    assert names == {"CustomerID", "FirstName", "SSN", "Balance"}
    # constraint line is not mistaken for a column, decimal(10,2) comma ignored
    assert all(t == "dbo.Customers" or t == "Customers" for t, _ in cols)


def test_columns_quoted_and_bracketed():
    sql = 'CREATE TABLE "public"."t" ("email" text, `phone` varchar(20), [ZipCode] int);'
    names = {c for _, c in columns_from_sql(sql)}
    assert names == {"email", "phone", "ZipCode"}


def test_columns_alter_add():
    sql = "ALTER TABLE Users ADD COLUMN credit_card VARCHAR(16);"
    assert ("Users", "credit_card") in columns_from_sql(sql)


def test_comments_ignored():
    sql = """
    -- CREATE TABLE fake ( ssn char );
    /* CREATE TABLE fake2 ( password text ); */
    CREATE TABLE real ( id int, notes text );
    """
    names = {c for _, c in columns_from_sql(sql)}
    assert names == {"id", "notes"}


def test_scan_sql_files_flags_high_pii(tmp_path):
    p = tmp_path / "001_init.sql"
    p.write_text("CREATE TABLE People (id int, ssn char(11), credit_card varchar(16));")
    findings = scan_sql_files([str(p)])
    cats = {f.column for f in findings}
    assert "ssn" in cats and "credit_card" in cats
    assert any(f.risk == "HIGH" for f in findings)
    # the file path is recorded in the schema slot
    assert all(f.schema == str(p) for f in findings)


def test_scan_sql_files_missing_file_skipped():
    assert scan_sql_files(["/no/such/file.sql"]) == []


# --- scan-files CLI ---------------------------------------------------------

def test_scan_files_command_clean(tmp_path):
    p = tmp_path / "safe.sql"
    p.write_text("CREATE TABLE widgets (id int, color varchar(20), qty int);")
    res = CliRunner().invoke(cli.scan_files, [str(p)])
    assert res.exit_code == 0
    assert "no PII columns" in res.output


def test_scan_files_fail_on_high(tmp_path):
    p = tmp_path / "bad.sql"
    p.write_text("CREATE TABLE t (id int, ssn char(11));")
    res = CliRunner().invoke(cli.scan_files, [str(p), "--fail-on", "high"])
    assert res.exit_code == 1
    assert "GATE FAILED" in res.output


def test_scan_files_json_output(tmp_path):
    p = tmp_path / "t.sql"
    p.write_text("CREATE TABLE t (email varchar(100));")
    out = tmp_path / "out.json"
    res = CliRunner().invoke(cli.scan_files, [str(p), "--json", str(out)])
    assert res.exit_code == 0 and out.exists()


# --- install-hooks ----------------------------------------------------------

def test_install_hooks_writes_pre_commit(tmp_path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    result = hooks.install_hooks(str(tmp_path))
    assert result["status"] == "installed"
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    body = hook.read_text()
    assert "sqldoc scan-files --fail-on high" in body
    assert hooks.HOOK_MARKER in body


def test_install_hooks_not_a_repo(tmp_path):
    result = hooks.install_hooks(str(tmp_path / "nope"))
    assert result["status"] == "not_a_repo"


def test_install_hooks_refuses_existing_foreign_hook(tmp_path):
    hd = tmp_path / ".git" / "hooks"
    hd.mkdir(parents=True)
    (hd / "pre-commit").write_text("#!/bin/sh\necho custom\n")
    result = hooks.install_hooks(str(tmp_path))
    assert result["status"] == "exists"
    # --force overwrites
    result2 = hooks.install_hooks(str(tmp_path), force=True)
    assert result2["status"] == "installed"


def test_install_hooks_cli(tmp_path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    res = CliRunner().invoke(cli.install_hooks, ["--repo", str(tmp_path)])
    assert res.exit_code == 0
    assert "Installed sqldoc pre-commit hook" in res.output
