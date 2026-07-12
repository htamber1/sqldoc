"""Multi-tenant REST API: per-tenant API keys + complete data isolation."""
import pytest

from sqldoc import api, cli
from sqldoc.adapters.base import Capabilities
from sqldoc.extractor import Table, Column


class _TenantAdapter:
    """A fake adapter whose tables are tagged with the tenant name, so a test
    can prove which tenant's data a request actually reached."""
    dialect = "sqlserver"
    display_name = "SQL Server"

    def __init__(self, tag):
        self.tag = tag
        self.capabilities = Capabilities(health=True, quality=True)

    def extract_metadata(self):
        return [Table("dbo", f"{self.tag}_People", 3, columns=[
            Column("Id", "int", 4, False, True, False, None, None)])]

    def extract_views(self):
        return []

    def extract_procedures(self):
        return []


@pytest.fixture
def mt_ctx(monkeypatch):
    # conn_str -> adapter tagged with the tenant so isolation is observable.
    adapters = {"csA": _TenantAdapter("Acme"), "csB": _TenantAdapter("Globex")}
    monkeypatch.setattr(api, "get_adapter", lambda cs, d=None: adapters[cs])
    return {"tenants": {
        "key-a": {"name": "Acme", "conn_str": "csA", "dialect": "sqlserver",
                  "database": "AcmeDB", "mode": "local", "model": None},
        "key-b": {"name": "Globex", "conn_str": "csB", "dialect": "sqlserver",
                  "database": "GlobexDB", "mode": "local", "model": None},
    }}


# --- auth + tenant selection ------------------------------------------------

def test_missing_key_401(mt_ctx):
    status, payload = api.dispatch("GET", "/api/doc", {}, {}, mt_ctx)
    assert status == 401


def test_unknown_key_401(mt_ctx):
    status, _ = api.dispatch("GET", "/api/doc", {"X-API-Key": "nope"}, {}, mt_ctx)
    assert status == 401


def test_tenant_a_sees_only_its_data(mt_ctx):
    status, payload = api.dispatch("GET", "/api/doc", {"X-API-Key": "key-a"}, {}, mt_ctx)
    assert status == 200
    assert payload["database"] == "AcmeDB"
    assert payload["tables"][0]["name"] == "Acme_People"


def test_tenant_b_sees_only_its_data(mt_ctx):
    status, payload = api.dispatch("GET", "/api/doc", {"X-API-Key": "key-b"}, {}, mt_ctx)
    assert status == 200
    assert payload["database"] == "GlobexDB"
    assert payload["tables"][0]["name"] == "Globex_People"


def test_isolation_key_a_cannot_reach_b(mt_ctx):
    # There is no path by which key-a's request touches csB / Globex data.
    _, payload = api.dispatch("GET", "/api/scan", {"X-API-Key": "key-a"}, {}, mt_ctx)
    assert payload["database"] == "AcmeDB"


def test_catalog_names_the_tenant(mt_ctx):
    status, payload = api.dispatch("GET", "/api", {"X-API-Key": "key-b"}, {}, mt_ctx)
    assert status == 200 and payload["multi_tenant"] is True
    assert payload["tenant"] == "Globex"


def test_agent_status_not_exposed_multitenant(mt_ctx):
    status, payload = api.dispatch("GET", "/api/agent/status", {"X-API-Key": "key-a"}, {}, mt_ctx)
    assert status == 200 and payload["running"] is False
    assert "multi-tenant" in payload["note"]


def test_handler_never_sees_tenant_registry(mt_ctx, monkeypatch):
    seen = {}

    def spy(adapter, ctx, params, body):
        seen["ctx"] = ctx
        return {"ok": True}
    monkeypatch.setitem(api.ENDPOINTS, ("GET", "/api/doc"), spy)
    api.dispatch("GET", "/api/doc", {"X-API-Key": "key-a"}, {}, mt_ctx)
    assert "tenants" not in seen["ctx"]           # registry stripped
    assert seen["ctx"]["conn_str"] == "csA"       # only this tenant's connection


# --- config loading (cli.load_tenants) --------------------------------------

def test_load_tenants_builds_registry():
    cfg = {"tenants": [
        {"name": "Acme", "api_key": "ka", "connection_string": "postgresql://h/acme", "dialect": "postgres"},
        {"name": "Globex", "api_key": "kb", "connection_string": "mysql://h/globex", "dialect": "mysql"},
    ]}
    reg = cli.load_tenants(cfg)
    assert set(reg) == {"ka", "kb"}
    assert reg["ka"]["name"] == "Acme" and reg["ka"]["dialect"] == "postgres"


def test_load_tenants_requires_key_and_name():
    import click
    with pytest.raises(click.UsageError):
        cli.load_tenants({"tenants": [{"name": "X", "connection_string": "c"}]})
    with pytest.raises(click.UsageError):
        cli.load_tenants({"tenants": [{"api_key": "k", "connection_string": "c"}]})


def test_load_tenants_rejects_duplicate_keys():
    import click
    cfg = {"tenants": [
        {"name": "A", "api_key": "same", "connection_string": "c1"},
        {"name": "B", "api_key": "same", "connection_string": "c2"},
    ]}
    with pytest.raises(click.UsageError):
        cli.load_tenants(cfg)


def test_load_tenants_empty_errors():
    import click
    with pytest.raises(click.UsageError):
        cli.load_tenants({})
