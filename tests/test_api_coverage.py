"""Cover the api.dispatch control paths: index, auth, multi-tenant, errors."""
import pytest

from sqldoc import api
from sqldoc import __version__


def test_index_endpoint():
    status, payload = api.dispatch("GET", "/api", {}, None, {"conn_str": "x"})
    assert status == 200 and payload["service"] == "sqldoc" and payload["version"] == __version__


def test_unknown_endpoint_404():
    status, payload = api.dispatch("GET", "/api/nope", {}, None, {"conn_str": "x"})
    assert status == 404 and "no endpoint" in payload["error"]


def test_missing_conn_str_400():
    status, payload = api.dispatch("GET", "/api/doc", {}, None, {})
    assert status == 400 and "no database" in payload["error"]


def test_api_key_required():
    ctx = {"api_key": "secret", "conn_str": "x"}
    status, _ = api.dispatch("GET", "/api/doc", {}, None, ctx)
    assert status == 401
    # correct key -> passes auth (then fails later at adapter, but not 401)
    status2, _ = api.dispatch("GET", "/api/doc", {"X-API-Key": "secret"}, None, ctx)
    assert status2 != 401


def test_sso_auth_path():
    class Authn:
        enabled = True

        def authenticate(self, headers):
            return (headers.get("Authorization") == "Bearer good", "bad token")
    ctx = {"authn": Authn(), "conn_str": "x"}
    assert api.dispatch("GET", "/api/doc", {}, None, ctx)[0] == 401
    ok = api.dispatch("GET", "/api/doc", {"Authorization": "Bearer good"}, None, ctx)
    assert ok[0] != 401


def test_multi_tenant_selection():
    tenants = {"k1": {"name": "t1", "conn_str": "c1"}, "k2": {"name": "t2", "conn_str": "c2"}}
    ctx = {"tenants": tenants}
    # no key -> 401
    assert api.dispatch("GET", "/api", {}, None, ctx)[0] == 401
    # valid key -> index shows tenant
    status, payload = api.dispatch("GET", "/api", {"X-API-Key": "k1"}, None, ctx)
    assert status == 200 and payload["tenant"] == "t1" and payload["multi_tenant"]
    # agent status not exposed across tenants
    status2, payload2 = api.dispatch("GET", "/api/agent/status", {"X-API-Key": "k1"}, None, ctx)
    assert status2 == 200 and "not exposed" in payload2["note"]


def test_value_error_becomes_400():
    # access-request endpoint without a user -> ValueError -> 400
    ctx = {"config": {"access": {"servers": []}}}
    status, payload = api.dispatch("POST", "/api/access/request", {}, {}, ctx)
    assert status == 400


def test_query_endpoint_needs_question():
    from sqldoc.api import _ep_query
    with pytest.raises(ValueError):
        _ep_query(None, {}, {}, {})


def test_agent_status_no_store(tmp_path, monkeypatch):
    from sqldoc.api import _ep_agent_status
    out = _ep_agent_status(None, {"agent_store": str(tmp_path / "absent.db")}, {}, None)
    assert out["running"] is False and out["databases"] == []
