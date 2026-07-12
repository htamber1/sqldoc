"""Shared helpers for the live-database integration tests.

Every integration test is *skip-gated*: it runs only when the target database is
actually reachable, and skips cleanly otherwise (so the suite still passes on a
machine without Docker). Connection strings can be overridden via env vars.
"""
import json
import os

import pytest
from click.testing import CliRunner


# --- connection targets (override via env) ---------------------------------

MSSQL_CS = os.environ.get(
    "SQLDOC_TEST_MSSQL",
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;"
    "DATABASE=AdventureWorks2022;UID=sa;PWD=SqlDoc123!;TrustServerCertificate=yes")

PG_CS = os.environ.get(
    "SQLDOC_TEST_PG", "postgresql://postgres:sqldoc@localhost:55432/pagila")

MYSQL_CS = os.environ.get(
    "SQLDOC_TEST_MYSQL", "mysql://root:sqldoc@localhost:33061/sakila")


def _probe_odbc(cs) -> bool:
    try:
        import pyodbc
        c = pyodbc.connect(cs, timeout=3)
        c.close()
        return True
    except Exception:
        return False


def _probe_pg(cs) -> bool:
    try:
        import psycopg2
        c = psycopg2.connect(cs, connect_timeout=3)
        c.close()
        return True
    except Exception:
        return False


def _probe_mysql(cs) -> bool:
    try:
        from sqldoc.adapters import get_adapter
        a = get_adapter(cs, "mysql")
        conn = a.connect()
        conn.close()
        return True
    except Exception:
        return False


MSSQL_AVAILABLE = _probe_odbc(MSSQL_CS)
PG_AVAILABLE = _probe_pg(PG_CS)
MYSQL_AVAILABLE = _probe_mysql(MYSQL_CS)

requires_mssql = pytest.mark.skipif(not MSSQL_AVAILABLE, reason="SQL Server not reachable")
requires_pg = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL/Pagila not reachable")
requires_mysql = pytest.mark.skipif(not MYSQL_AVAILABLE, reason="MySQL/Sakila not reachable")


# --- CLI runner ------------------------------------------------------------

def run(args) -> object:
    """Invoke the sqldoc CLI with args; return the click Result."""
    from sqldoc import cli
    return CliRunner().invoke(cli.cli, args, catch_exceptions=False)


def read_json(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
