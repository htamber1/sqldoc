"""Live validation of the mock-only database adapters.

Each cloud/enterprise dialect is unit-tested with a driver-shaped fake but has
NOT been run against a live instance (no account/license in CI). Set the matching
``SQLDOC_TEST_<DIALECT>`` env var to a real connection string and this validates
that `sqldoc doc` extracts real metadata end-to-end. Commands beyond `doc`
(scan/intel/insights are dialect-neutral; health/quality need the
`quality`/`health` capability) are noted in LIVE_TESTING.md.

Examples of the connection strings each expects:
  SQLDOC_TEST_SNOWFLAKE   snowflake://user:pw@account/DB/SCHEMA?warehouse=WH&role=R
  SQLDOC_TEST_ORACLE      oracle://user:pw@host:1521/servicename
  SQLDOC_TEST_REDSHIFT    postgresql://user:pw@cluster.xxx.redshift.amazonaws.com:5439/db
  SQLDOC_TEST_DATABRICKS  databricks://token:<tok>@adb-x.azuredatabricks.net/sql/1.0/warehouses/xxx?catalog=main
  SQLDOC_TEST_BIGQUERY    bigquery://project/dataset
  SQLDOC_TEST_COCKROACHDB postgresql://user:pw@host:26257/db?sslmode=verify-full
  SQLDOC_TEST_DB2         db2://user:pw@host:50000/db
  SQLDOC_TEST_MONGODB     mongodb+srv://user:pw@cluster.mongodb.net/db
  SQLDOC_TEST_AZURESQL    Driver=...;Server=x.database.windows.net;Database=db;Uid=..;Pwd=..
  SQLDOC_TEST_AZURE_MI    Driver=...;Server=x.<zone>.database.windows.net;Database=db;Uid=..;Pwd=..
  SQLDOC_TEST_SYNAPSE     Driver=...;Server=x.sql.azuresynapse.net;Database=pool;Uid=..;Pwd=..
  SQLDOC_TEST_AURORA_PG   postgresql://user:pw@cluster.cluster-xxx.rds.amazonaws.com/db
  SQLDOC_TEST_AURORA_MYSQL mysql://user:pw@cluster.cluster-xxx.rds.amazonaws.com/db
"""
import json
import os

import pytest

from _liveutil import run

pytestmark = pytest.mark.live


# (env var, --dialect value, pip extra needed)
DIALECTS = [
    ("SQLDOC_TEST_AZURESQL",     "azuresql",              "pyodbc (core)"),
    ("SQLDOC_TEST_AZURE_MI",     "azure_managed_instance", "pyodbc (core)"),
    ("SQLDOC_TEST_SYNAPSE",      "synapse",               "pyodbc (core)"),
    ("SQLDOC_TEST_SNOWFLAKE",    "snowflake",             "sqldoc[snowflake]"),
    ("SQLDOC_TEST_ORACLE",       "oracle",                "sqldoc[oracle]"),
    ("SQLDOC_TEST_REDSHIFT",     "redshift",              "sqldoc[postgres]"),
    ("SQLDOC_TEST_DATABRICKS",   "databricks",            "sqldoc[databricks]"),
    ("SQLDOC_TEST_BIGQUERY",     "bigquery",              "sqldoc[bigquery]"),
    ("SQLDOC_TEST_COCKROACHDB",  "cockroachdb",           "sqldoc[postgres]"),
    ("SQLDOC_TEST_DB2",          "db2",                   "sqldoc[db2]"),
    ("SQLDOC_TEST_MONGODB",      "mongodb",               "sqldoc[mongodb]"),
    ("SQLDOC_TEST_AURORA_PG",    "aurora_postgres",       "sqldoc[postgres]"),
    ("SQLDOC_TEST_AURORA_MYSQL", "aurora_mysql",          "sqldoc[mysql]"),
]


@pytest.mark.parametrize("env_var,dialect,extra", DIALECTS,
                         ids=[d[1] for d in DIALECTS])
def test_doc_extracts_real_metadata(tmp_path, env_var, dialect, extra):
    cs = os.environ.get(env_var)
    if not cs:
        pytest.skip(f"set {env_var} to a live {dialect} connection string "
                    f"(needs {extra}) to validate this adapter")
    out = str(tmp_path / f"{dialect}.json")
    r = run(["doc", "--connection-string", cs, "--dialect", dialect,
             "--no-ai", "--format", "json", "--output", out], with_config=False)
    assert r.exit_code == 0, f"{dialect} doc failed:\n{r.output}"
    with open(out, encoding="utf-8") as f:
        data = json.load(f)
    tables = data.get("tables", data if isinstance(data, list) else [])
    assert tables, f"{dialect}: no tables/collections extracted — check the account has objects"
    print(f"\n[{dialect}] extracted {len(tables)} tables/collections from the live instance")


@pytest.mark.parametrize("env_var,dialect,extra",
                         [d for d in DIALECTS if d[1] in
                          ("snowflake", "oracle", "redshift", "databricks",
                           "bigquery", "cockroachdb", "db2", "mongodb",
                           "azuresql", "azure_managed_instance", "synapse",
                           "aurora_postgres", "aurora_mysql")],
                         ids=lambda d: d if isinstance(d, str) else None)
def test_scan_runs_dialect_neutral(tmp_path, env_var, dialect, extra):
    """PII scan is dialect-neutral (runs on extracted metadata), so it should
    work on every adapter that `doc` works on."""
    cs = os.environ.get(env_var)
    if not cs:
        pytest.skip(f"set {env_var} to validate scan on live {dialect}")
    out = str(tmp_path / f"{dialect}-scan.json")
    r = run(["scan", "--connection-string", cs, "--dialect", dialect,
             "--json", out], with_config=False)
    assert r.exit_code == 0, f"{dialect} scan failed:\n{r.output}"
    print(f"\n[{dialect}] PII scan completed against the live instance")
