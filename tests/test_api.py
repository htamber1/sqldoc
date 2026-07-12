"""REST API: dispatch routing, auth, endpoints, and a live-server smoke test."""
import json
import threading
import urllib.request

import pytest

from sqldoc import api
from sqldoc.adapters.base import Capabilities
from sqldoc.extractor import Table, Column
from conftest import FakeConnection, FakeRow


class _ApiAdapter:
    dialect = "sqlserver"
    display_name = "SQL Server"

    def __init__(self, tables, rows):
        self._t, self._rows = tables, rows
        self.capabilities = Capabilities(health=True, quality=True, access_audit=True,
                                         server_monitoring=True, infra_monitoring=True)

    def extract_metadata(self):
        return self._t

    def extract_views(self):
        return []

    def extract_procedures(self):
        return []

    def connect(self):
        return FakeConnection(self._rows)

    def cursor(self, conn):
        return conn.cursor()


def _tables():
    return [Table("dbo", "People", 10, columns=[
        Column("Id", "int", 4, False, True, False, None, None),
        Column("EmailAddress", "nvarchar", 50, True, False, False, None, None)])]


@pytest.fixture
def ctx(monkeypatch):
    secure_rows = {"mssql_logins": [FakeRow(name="sa", is_disabled=0, blank_pw=0)],
                   "mssql_config": [], "mssql_trustworthy": [], "mssql_public": [FakeRow(n=0)]}
    adapter = _ApiAdapter(_tables(), secure_rows)
    monkeypatch.setattr(api, "get_adapter", lambda cs, d=None: adapter)
    return {"conn_str": "cs", "dialect": "sqlserver", "database": "SalesDB",
            "api_key": None, "mode": "local", "model": None, "agent_store": "/no/such/store.db"}


# --- routing + catalog ------------------------------------------------------

def test_catalog_lists_endpoints(ctx):
    status, payload = api.dispatch("GET", "/api", {}, {}, ctx)
    assert status == 200 and payload["service"] == "sqldoc"
    assert "GET /api/doc" in payload["endpoints"]


def test_unknown_endpoint_404(ctx):
    status, payload = api.dispatch("GET", "/api/nope", {}, {}, ctx)
    assert status == 404 and "error" in payload


# --- auth -------------------------------------------------------------------

def test_auth_required_when_key_set(ctx):
    ctx["api_key"] = "secret"
    status, payload = api.dispatch("GET", "/api/doc", {}, {}, ctx)
    assert status == 401
    status, _ = api.dispatch("GET", "/api/doc", {"X-API-Key": "wrong"}, {}, ctx)
    assert status == 401
    status, _ = api.dispatch("GET", "/api/doc", {"X-API-Key": "secret"}, {}, ctx)
    assert status == 200


def test_open_when_no_key(ctx):
    status, _ = api.dispatch("GET", "/api/doc", {}, {}, ctx)
    assert status == 200


# --- endpoints --------------------------------------------------------------

def test_doc_endpoint(ctx):
    status, payload = api.dispatch("GET", "/api/doc", {}, {}, ctx)
    assert status == 200 and payload["database"] == "SalesDB"
    assert payload["tables"][0]["name"] == "People"


def test_scan_endpoint(ctx):
    status, payload = api.dispatch("GET", "/api/scan", {}, {}, ctx)
    assert status == 200
    assert any(f["column"] == "EmailAddress" for f in payload["findings"])


def test_secure_endpoint(ctx):
    status, payload = api.dispatch("GET", "/api/secure", {}, {}, ctx)
    assert status == 200 and payload["report_type"] == "security"
    assert "score" in payload["summary"]


def test_query_endpoint(ctx, monkeypatch):
    from sqldoc.insights import QueryResult
    monkeypatch.setattr("sqldoc.insights.answer_question",
                        lambda q, tables, mode="local", model=None: QueryResult(question=q, sql="SELECT 1"))
    status, payload = api.dispatch("POST", "/api/query", {}, {"question": "how many people?"}, ctx)
    assert status == 200 and payload["sql"] == "SELECT 1"


def test_query_missing_question_400(ctx):
    status, payload = api.dispatch("POST", "/api/query", {}, {}, ctx)
    assert status == 400 and "question" in payload["error"]


def test_agent_status_no_store(ctx):
    status, payload = api.dispatch("GET", "/api/agent/status", {}, {}, ctx)
    assert status == 200 and payload["running"] is False


def test_endpoint_needs_connection():
    # no conn_str configured -> 400 for adapter-backed endpoints
    ctx = {"conn_str": None, "api_key": None}
    status, payload = api.dispatch("GET", "/api/doc", {}, {}, ctx)
    assert status == 400


# --- live HTTP server smoke test -------------------------------------------

def test_live_server(monkeypatch):
    adapter = _ApiAdapter(_tables(), {})
    monkeypatch.setattr(api, "get_adapter", lambda cs, d=None: adapter)
    ctx = {"conn_str": "cs", "dialect": "sqlserver", "database": "SalesDB", "api_key": "k"}
    httpd = api.make_server("127.0.0.1", 0, ctx)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # unauthenticated -> 401
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/doc")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
        # authenticated -> 200 with JSON
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/doc",
                                     headers={"X-API-Key": "k"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["tables"][0]["name"] == "People"
    finally:
        httpd.shutdown()
