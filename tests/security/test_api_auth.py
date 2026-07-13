"""Security: the REST API requires auth, isolates tenants, hides internals,
sends security headers, and rate-limits."""
from sqldoc import api


def test_requires_api_key_when_configured():
    ctx = {"conn_str": "x", "api_key": "secret"}
    assert api.dispatch("GET", "/api/doc", {}, {}, ctx)[0] == 401
    assert api.dispatch("GET", "/api/doc", {"X-API-Key": "wrong"}, {}, ctx)[0] == 401


def test_constant_time_key_compare():
    assert api._key_matches("abc", "abc") is True
    assert api._key_matches("abc", "abd") is False
    assert api._key_matches(None, "abc") is False
    assert api._key_matches("abc", None) is False
    assert api._key_matches("", "") is False


def test_multitenant_key_selects_and_isolates():
    mt = {"tenants": {"key-a": {"name": "A", "database": "dba", "conn_str": "a"},
                      "key-b": {"name": "B", "database": "dbb", "conn_str": "b"}}}
    # No/invalid key -> 401.
    assert api.dispatch("GET", "/api", {}, {}, mt)[0] == 401
    assert api.dispatch("GET", "/api", {"X-API-Key": "nope"}, {}, mt)[0] == 401
    # Each key only sees its own tenant.
    _, a = api.dispatch("GET", "/api", {"X-API-Key": "key-a"}, {}, mt)
    _, b = api.dispatch("GET", "/api", {"X-API-Key": "key-b"}, {}, mt)
    assert a["tenant"] == "A" and b["tenant"] == "B"
    # agent status is not exposed across tenants.
    status, payload = api.dispatch("GET", "/api/agent/status",
                                   {"X-API-Key": "key-a"}, {}, mt)
    assert "not exposed" in payload.get("note", "")


def test_unexpected_error_returns_generic_500(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret path /etc/shadow")
    monkeypatch.setitem(api.ENDPOINTS, ("GET", "/api/doc"), boom)
    monkeypatch.setattr(api, "get_adapter", lambda *a, **k: object())
    status, payload = api.dispatch("GET", "/api/doc", {}, {}, {"conn_str": "x"})
    assert status == 500
    assert payload == {"error": "internal server error"}
    assert "shadow" not in str(payload)


def test_security_headers_defined():
    for h in ("X-Content-Type-Options", "X-Frame-Options",
              "Content-Security-Policy", "Referrer-Policy", "Cache-Control"):
        assert h in api._SECURITY_HEADERS
    assert api._SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert api._SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    # CORS must not be wildcarded open.
    assert "Access-Control-Allow-Origin" not in api._SECURITY_HEADERS


def test_rate_limiter_blocks_over_limit():
    rl = api.RateLimiter(max_requests=3, window_seconds=60)
    assert [rl.allow("1.1.1.1", now=0) for _ in range(5)] == [True, True, True, False, False]
    # Different client is independent; window reset lets it through again.
    assert rl.allow("2.2.2.2", now=0) is True
    assert rl.allow("1.1.1.1", now=61) is True
