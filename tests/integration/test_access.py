"""Phase 8 — access management against the live SQL Server.

Creates a real SQL login + database user + role membership, then runs
`access check / request / script / review` against the live catalog (using the
native identity source — no external directory). The generated grant script is
validated as syntactically valid T-SQL via SET PARSEONLY against the server.
Skip-gated; cleans up the login/user on teardown."""
import json
import os
import re

import pytest
import yaml

from _live import MSSQL_CS, requires_mssql, run

pytestmark = [requires_mssql, pytest.mark.integration]

LOGIN = "sqldoc_test_user"
DBNAME = "AdventureWorks2022"


def _master_cs():
    return re.sub(r'(?i)(DATABASE|Initial\s+Catalog)\s*=[^;]*', 'DATABASE=master', MSSQL_CS)


def _exec(cs, *statements):
    import pyodbc
    conn = pyodbc.connect(cs, timeout=10, autocommit=True)
    try:
        cur = conn.cursor()
        for s in statements:
            cur.execute(s)
    finally:
        conn.close()


@pytest.fixture
def access_env(tmp_path):
    # Create a real login + db user + db_datareader membership.
    _exec(_master_cs(),
          f"IF SUSER_ID('{LOGIN}') IS NULL CREATE LOGIN [{LOGIN}] "
          f"WITH PASSWORD = N'SqlDoc123!Tmp', CHECK_POLICY = OFF")
    _exec(MSSQL_CS,
          f"IF DATABASE_PRINCIPAL_ID('{LOGIN}') IS NULL CREATE USER [{LOGIN}] FOR LOGIN [{LOGIN}]",
          f"ALTER ROLE db_datareader ADD MEMBER [{LOGIN}]")
    cfg = {"access": {
        "ad": {"type": "native"},
        "servers": [{"name": "aw", "connection_string": MSSQL_CS,
                     "dialect": "sqlserver", "databases": [DBNAME]}],
    }}
    path = tmp_path / ".sqldoc.yml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    try:
        yield str(path)
    finally:
        _exec(MSSQL_CS, f"IF DATABASE_PRINCIPAL_ID('{LOGIN}') IS NOT NULL DROP USER [{LOGIN}]")
        _exec(_master_cs(), f"IF SUSER_ID('{LOGIN}') IS NOT NULL DROP LOGIN [{LOGIN}]")


# --- check -----------------------------------------------------------------

def test_access_check_finds_real_login(access_env, tmp_path):
    js = str(tmp_path / "check.json")
    r = run(["access", "check", "--config", access_env, "--user", LOGIN,
             "--output", str(tmp_path / "check.html"), "--json", js])
    assert r.exit_code == 0, r.output
    data = json.loads(open(js, encoding="utf-8").read())
    assert data["report_type"] == "access-check"
    dbs = [a for a in data["access"] if a["database"] == DBNAME]
    assert dbs, "the test login's DB access was not found"
    assert "db_datareader" in dbs[0]["roles"] and dbs[0]["level"] == "read"


# --- request ---------------------------------------------------------------

def test_access_request_partial(access_env, tmp_path):
    js = str(tmp_path / "req.json")
    r = run(["access", "request", "--config", access_env, "--user", LOGIN,
             "--request", f"write access to the {DBNAME} database", "--no-ai",
             "--output", str(tmp_path / "req.html"), "--json", js])
    assert r.exit_code == 0, r.output
    data = json.loads(open(js, encoding="utf-8").read())
    # already has read via db_datareader, needs write -> PARTIAL
    assert data["verdict"] == "PARTIAL"
    assert data["have_level"] == "read" and data["needs_level"] == "write"


# --- script: generated T-SQL is valid --------------------------------------

def _parseonly_ok(sql_batches):
    """Validate each batch parses as T-SQL via SET PARSEONLY (no execution)."""
    import pyodbc
    conn = pyodbc.connect(_master_cs(), timeout=10, autocommit=True)
    try:
        cur = conn.cursor()
        for batch in sql_batches:
            if not batch.strip():
                continue
            cur.execute("SET PARSEONLY ON")
            try:
                cur.execute(batch)
            finally:
                cur.execute("SET PARSEONLY OFF")
    finally:
        conn.close()


def test_access_script_generates_valid_tsql(access_env, tmp_path):
    sql_out = str(tmp_path / "grant.sql")
    r = run(["access", "script", "--config", access_env, "--user", LOGIN,
             "--database", DBNAME, "--level", "write", "--no-ai",
             "--output", str(tmp_path / "script.html"), "--sql-out", sql_out])
    assert r.exit_code == 0, r.output
    grant = open(sql_out, encoding="utf-8").read()
    assert "ALTER ROLE [db_datawriter] ADD MEMBER" in grant
    # split on GO and validate every batch parses on the live server
    batches = re.split(r"(?im)^\s*GO\s*$", grant)
    _parseonly_ok(batches)
    # the rollback script also parses
    rollback = open(sql_out.rsplit(".", 1)[0] + ".rollback.sql", encoding="utf-8").read()
    _parseonly_ok(re.split(r"(?im)^\s*GO\s*$", rollback))


# --- review ----------------------------------------------------------------

def test_access_review_runs(access_env, tmp_path):
    js = str(tmp_path / "review.json")
    r = run(["access", "review", "--config", access_env,
             "--output", str(tmp_path / "review.html"), "--json", js])
    assert r.exit_code == 0, r.output
    data = json.loads(open(js, encoding="utf-8").read())
    assert data["report_type"] == "access-review"
    assert "findings" in data and isinstance(data["findings"], list)
