"""Phase 7 — agent integration against the live SQL Server.

Creates a small isolated test database, runs the agent's poll cycle in-process,
verifies the store is populated, the dashboard serves every route, and schema-
change detection fires when a table is altered between polls. Also exercises the
capacity command (which reads the agent store). Skips when SQL Server is
unreachable; drops the test database on teardown."""
import re
import threading
import urllib.request

import pytest

from _live import MSSQL_CS, MSSQL_AVAILABLE, requires_mssql, run

pytestmark = [requires_mssql, pytest.mark.integration]

TEST_DB = "sqldoc_agent_it"


def _master_cs():
    return re.sub(r'(?i)(DATABASE|Initial\s+Catalog)\s*=[^;]*', 'DATABASE=master', MSSQL_CS)


def _testdb_cs():
    return re.sub(r'(?i)(DATABASE|Initial\s+Catalog)\s*=[^;]*', f'DATABASE={TEST_DB}', MSSQL_CS)


def _exec(cs, *statements, autocommit=True):
    import pyodbc
    conn = pyodbc.connect(cs, timeout=10, autocommit=autocommit)
    try:
        cur = conn.cursor()
        for s in statements:
            cur.execute(s)
        if not autocommit:
            conn.commit()
    finally:
        conn.close()


@pytest.fixture
def test_db():
    """A fresh isolated database with one PII-bearing table."""
    _exec(_master_cs(),
          f"IF DB_ID('{TEST_DB}') IS NOT NULL BEGIN ALTER DATABASE {TEST_DB} SET SINGLE_USER "
          f"WITH ROLLBACK IMMEDIATE; DROP DATABASE {TEST_DB}; END",
          f"CREATE DATABASE {TEST_DB}")
    _exec(_testdb_cs(),
          "CREATE TABLE dbo.Widget (Id int PRIMARY KEY, Name varchar(50), Email varchar(100))",
          "INSERT INTO dbo.Widget VALUES (1,'a','a@x.com'),(2,'b','b@x.com')")
    try:
        yield _testdb_cs()
    finally:
        _exec(_master_cs(),
              f"IF DB_ID('{TEST_DB}') IS NOT NULL BEGIN ALTER DATABASE {TEST_DB} SET SINGLE_USER "
              f"WITH ROLLBACK IMMEDIATE; DROP DATABASE {TEST_DB}; END")


def _agent(cs):
    from sqldoc.agent.store import AgentStore
    from sqldoc.agent import db_path
    from sqldoc.agent.config import AgentConfig, DatabaseConfig, NotifyConfig
    from sqldoc.agent.notify import Notifier
    from sqldoc.agent.poller import poll_database
    store = AgentStore(db_path())
    db = DatabaseConfig(name="widgetdb", connection_string=cs, dialect="sqlserver", no_ai=True)
    ac = AgentConfig(databases=[db], no_ai=True)
    notifier = Notifier(NotifyConfig(on=[]))
    return store, db, ac, notifier, poll_database


# --- poll populates the store ----------------------------------------------

def test_poll_populates_store(test_db):
    store, db, ac, notifier, poll = _agent(test_db)
    res = poll(store, db, ac, notifier)
    assert res["status"] == "ok"
    assert "widgetdb" in store.list_databases()
    metric = store.latest_metric("widgetdb")
    assert metric and metric["tables"] == 1
    run_row = store.last_run("widgetdb")
    assert run_row and run_row["status"] == "ok"
    doc_html, _ = store.get_doc("widgetdb")
    assert doc_html and "Widget" in doc_html
    # the Email column produced a PII finding -> pii metric present
    assert metric.get("pii_high", 0) + metric.get("pii_medium", 0) + metric.get("pii_low", 0) >= 1


# --- schema-change detection -----------------------------------------------

def test_schema_change_detected(test_db):
    store, db, ac, notifier, poll = _agent(test_db)
    first = poll(store, db, ac, notifier)
    assert first["schema_changed"] is False        # baseline snapshot

    _exec(test_db, "ALTER TABLE dbo.Widget ADD Note varchar(200) NULL")
    second = poll(store, db, ac, notifier)
    assert second["schema_changed"] is True
    events = store.recent_events("widgetdb")
    assert any(e["type"] == "schema_change" for e in events)


# --- dashboard serves every route ------------------------------------------

def test_dashboard_routes(test_db):
    from sqldoc.agent.dashboard import make_server
    store, db, ac, notifier, poll = _agent(test_db)
    poll(store, db, ac, notifier)

    server = make_server(store, 0, "127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        def get(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return r.status, r.read().decode("utf-8", "replace")

        s, body = get("/")
        assert s == 200 and "widgetdb" in body
        s, body = get("/db/widgetdb")
        assert s == 200 and "widgetdb" in body
        s, body = get("/db/widgetdb/doc")
        assert s == 200 and "Widget" in body
        s, body = get("/api/overview")
        assert s == 200 and "widgetdb" in body
        s, body = get("/alerts")
        assert s == 200
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


# --- capacity reads the agent store ----------------------------------------

def test_capacity_after_polls(test_db):
    store, db, ac, notifier, poll = _agent(test_db)
    poll(store, db, ac, notifier)
    poll(store, db, ac, notifier)     # >= 2 cycles -> capacity has history
    # capacity reads the agent store (db_path), not a live connection.
    r = run(["capacity", "--database", "widgetdb", "--output",
             __import__("tempfile").mktemp(suffix=".html")])
    assert r.exit_code == 0, r.output
