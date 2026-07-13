"""Local REST API server — expose sqldoc commands as JSON HTTP endpoints.

``sqldoc serve --api`` starts a small stdlib HTTP server (no extra dependency) so
other tools and dashboards can call sqldoc programmatically:

* ``GET  /api``               — list available endpoints + version
* ``GET  /api/doc``           — full schema documentation (JSON)
* ``GET  /api/scan``          — PII / compliance findings
* ``GET  /api/health``        — DMV health report
* ``GET  /api/secure``        — security scan + score
* ``GET  /api/server``        — instance health (SQL Server family)
* ``GET  /api/waits``         — wait statistics
* ``GET  /api/plans``         — worst query plans
* ``GET  /api/ha``            — HA / replication status
* ``GET  /api/backup``        — backup / PITR status
* ``POST /api/query``         — natural-language → SQL (body: {"question": "..."})
* ``GET  /api/agent/status``  — background-agent status from the local store

All responses are JSON. Requests are authenticated with an ``X-API-Key`` header
matching the ``api_key`` configured in ``.sqldoc.yml`` (when one is set). The
target database comes from the same config / CLI connection settings the server
was started with. Reads only metadata/statistics — never table row data.
"""
import hmac
import json
import logging
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqldoc import __version__
from sqldoc.adapters import get_adapter

_log = logging.getLogger("sqldoc.api")

# Cap request bodies so a large/absent Content-Length can't exhaust memory.
_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

# Security response headers applied to every response. Responses can carry
# schema/PII data, so they must not be sniffed, framed, cached, or embedded.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
# NOTE: no `Access-Control-Allow-Origin` is ever sent — CORS is intentionally
# disabled so a browser on another origin cannot read these authenticated
# JSON responses. Do not add a wildcard ACAO here.


def _key_matches(provided, expected) -> bool:
    """Constant-time API-key comparison (avoids a timing side-channel on the
    key). False if either side is missing."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


class RateLimiter:
    """Tiny thread-safe fixed-window per-client rate limiter. Not distributed —
    one process, in-memory — which matches the single-process stdlib server."""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60):
        self.max = max_requests
        self.window = window_seconds
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, client: str, now: float = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            start, count = self._hits.get(client, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            count += 1
            self._hits[client] = (start, count)
            # Opportunistic cleanup so the map can't grow unbounded.
            if len(self._hits) > 4096:
                self._hits = {k: v for k, v in self._hits.items()
                              if now - v[0] < self.window}
            return count <= self.max


# --- endpoint handlers (adapter, ctx, params, body) -> dict -----------------

def _database(ctx):
    return ctx.get("database") or "database"


def _ep_doc(adapter, ctx, params, body):
    return {
        "database": _database(ctx),
        "tables": [asdict(t) for t in adapter.extract_metadata()],
        "views": [asdict(v) for v in adapter.extract_views()],
        "procedures": [asdict(p) for p in adapter.extract_procedures()],
    }


def _ep_scan(adapter, ctx, params, body):
    from sqldoc.pii import scan_tables, findings_json
    findings = scan_tables(adapter.extract_metadata())
    return findings_json(_database(ctx), findings)


def _ep_health(adapter, ctx, params, body):
    from sqldoc.health import collect_health
    from sqldoc.health_renderer import build_health_json
    tables = adapter.extract_metadata()
    report = collect_health(adapter, tables=tables)
    report.database = _database(ctx)
    return build_health_json(_database(ctx), report)


def _ep_secure(adapter, ctx, params, body):
    from sqldoc.secure import collect_security
    from sqldoc.secure_renderer import build_secure_json
    return build_secure_json(_database(ctx), collect_security(adapter))


def _ep_server(adapter, ctx, params, body):
    from sqldoc.server import collect_server
    from sqldoc.server_renderer import build_server_json
    report = collect_server(adapter)
    report.server_name = _database(ctx)
    return build_server_json(_database(ctx), report)


def _ep_waits(adapter, ctx, params, body):
    from sqldoc.waits import collect_waits
    from sqldoc.waits_renderer import build_waits_json
    return build_waits_json(_database(ctx), collect_waits(adapter))


def _ep_plans(adapter, ctx, params, body):
    from sqldoc.plans import collect_plans
    from sqldoc.plans_renderer import build_plans_json
    return build_plans_json(_database(ctx), collect_plans(adapter))


def _ep_ha(adapter, ctx, params, body):
    from sqldoc.ha import collect_ha
    from sqldoc.ha_renderer import build_ha_json
    return build_ha_json(_database(ctx), collect_ha(adapter))


def _ep_backup(adapter, ctx, params, body):
    from sqldoc.backup import collect_backups, summarize
    report = collect_backups(adapter)
    return {"database": _database(ctx), "summary": summarize(report),
            "pitr_enabled": report.pitr_enabled, "pitr_mechanism": report.pitr_mechanism,
            "databases": [asdict(d) for d in report.databases], "notes": report.notes}


def _ep_query(adapter, ctx, params, body):
    question = (body or {}).get("question")
    if not question:
        raise ValueError("POST body must be JSON with a 'question' field.")
    from sqldoc.insights import answer_question
    tables = adapter.extract_metadata()
    res = answer_question(question, tables, mode=ctx.get("mode", "local"), model=ctx.get("model"))
    return {"question": res.question, "sql": res.sql}


def _ep_agent_status(adapter, ctx, params, body):
    import os
    from sqldoc.agent.store import AgentStore
    from sqldoc.agent import db_path
    path = ctx.get("agent_store") or db_path()
    if not os.path.exists(path):
        return {"running": False, "store": path, "databases": []}
    store = AgentStore(path)
    out = []
    for name in store.list_databases():
        out.append({"database": name, "last_run": store.last_run(name),
                    "latest_metric": store.latest_metric(name)})
    return {"running": True, "store": path, "databases": out}


def _ep_access_request(adapter, ctx, params, body):
    """REST intake for the access suite: external systems POST an access request
    and get back the gap verdict + generated grant/rollback script."""
    cfg = ctx.get("config") or {}
    body = body or {}
    user = body.get("user") or body.get("identifier")
    if not user:
        raise ValueError("POST body must be JSON with a 'user' (and 'request' or "
                         "'database'+'level').")
    from sqldoc.access import config as access_config
    known = [db for s in access_config.servers(cfg) for db in s["databases"]]
    if body.get("request"):
        from sqldoc.access.parse import parse_request
        parsed = parse_request(body["request"], known_databases=known,
                               mode=ctx.get("mode", "local"), no_ai=body.get("no_ai", False))
        database, level = parsed.database, parsed.level
    else:
        database, level = body.get("database"), body.get("level", "read")
    if not database:
        raise ValueError("Could not determine the target database (pass 'database' or a clearer 'request').")
    from sqldoc.access.intake import run_request
    from sqldoc.access.render import build_script_json
    outcome = run_request(cfg, user, database, level, mode=ctx.get("mode", "local"))
    return {"user": user, "database": database, "level": level,
            "verdict": outcome.gap.verdict, "explanation": outcome.gap.explanation,
            "missing": outcome.gap.missing, "script": build_script_json(outcome.script)}


ENDPOINTS = {
    ("GET", "/api/doc"): _ep_doc,
    ("GET", "/api/scan"): _ep_scan,
    ("GET", "/api/health"): _ep_health,
    ("GET", "/api/secure"): _ep_secure,
    ("GET", "/api/server"): _ep_server,
    ("GET", "/api/waits"): _ep_waits,
    ("GET", "/api/plans"): _ep_plans,
    ("GET", "/api/ha"): _ep_ha,
    ("GET", "/api/backup"): _ep_backup,
    ("POST", "/api/query"): _ep_query,
    ("POST", "/api/access/request"): _ep_access_request,
    ("GET", "/api/agent/status"): _ep_agent_status,
}

# Endpoints that don't need a database connection (they use ctx["config"] or the store).
_NO_ADAPTER = {("GET", "/api/agent/status"), ("POST", "/api/access/request")}


# --- dispatch (pure, socket-free — easy to test) ---------------------------

def _provided_key(headers):
    return headers.get("X-API-Key") or headers.get("x-api-key")


def dispatch(method, path, headers, body, ctx) -> tuple:
    """Return (status_code, dict). `headers` is a case-insensitive-ish mapping.

    Single-tenant: `ctx` carries conn_str/dialect/database/api_key/mode/model.
    Multi-tenant: `ctx["tenants"]` maps each API key to its own isolated
    sub-context; the X-API-Key header selects the tenant and the request runs
    ONLY against that tenant's connection — a key can never reach another
    tenant's database.
    """
    path = path.rstrip("/") or "/api"
    tenants = ctx.get("tenants")

    if tenants is not None:
        # Multi-tenant: the API key IS the tenant selector (and is mandatory).
        tenant = tenants.get(_provided_key(headers))
        if tenant is None:
            return 401, {"error": "invalid or missing X-API-Key header"}
        # Effective per-request context: the tenant's own connection wins; drop
        # the tenant registry so no handler can see other tenants.
        req_ctx = {k: v for k, v in ctx.items() if k != "tenants"}
        req_ctx.update(tenant)
        # Agent status is operator-level, not tenant data — do not expose it
        # across tenants.
        if (method, path) == ("GET", "/api/agent/status"):
            return 200, {"running": False,
                         "note": "agent status is not exposed in multi-tenant mode"}
    else:
        # Single-tenant: an API key and/or SSO (OIDC/SAML). Either credential
        # satisfies the request; auth is required if either is configured.
        api_key = ctx.get("api_key")
        authn = ctx.get("authn")
        sso_on = authn is not None and getattr(authn, "enabled", False)
        if api_key or sso_on:
            authed = _key_matches(_provided_key(headers), api_key)
            err = "invalid or missing X-API-Key header"
            if not authed and sso_on:
                ok, result = authn.authenticate(headers)
                authed = ok
                if not ok:
                    err = result
            if not authed:
                return 401, {"error": err}
        req_ctx = ctx

    if method == "GET" and path in ("/api", "/"):
        payload = {"service": "sqldoc", "version": __version__,
                   "endpoints": [f"{m} {p}" for (m, p) in ENDPOINTS]}
        if tenants is not None:
            payload["tenant"] = req_ctx.get("name") or req_ctx.get("database")
            payload["multi_tenant"] = True
        return 200, payload

    handler = ENDPOINTS.get((method, path))
    if handler is None:
        return 404, {"error": f"no endpoint for {method} {path}"}

    try:
        adapter = None
        if (method, path) not in _NO_ADAPTER:
            if not req_ctx.get("conn_str"):
                return 400, {"error": "the server has no database configured"}
            adapter = get_adapter(req_ctx["conn_str"], req_ctx.get("dialect"))
        return 200, handler(adapter, req_ctx, {}, body)
    except ValueError as e:
        # ValueError is used for caller/input problems — safe to echo.
        return 400, {"error": str(e)}
    except Exception:
        # Never leak exception type/message (may contain paths, SQL, or
        # connection details). Log server-side; return a generic message.
        _log.exception("Unhandled error serving %s %s", method, path)
        return 500, {"error": "internal server error"}


# --- HTTP server -----------------------------------------------------------

def make_handler(ctx, rate_limiter=None):
    limiter = rate_limiter

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status, payload):
            data = json.dumps(payload, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _client(self):
            try:
                return self.client_address[0]
            except Exception:
                return "?"

        def _rate_ok(self):
            if limiter is None:
                return True
            if limiter.allow(self._client()):
                return True
            self._respond(429, {"error": "rate limit exceeded; slow down"})
            return False

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                return None  # malformed length
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                return None  # too large — signalled to caller
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            if not self._rate_ok():
                return
            path = self.path.split("?")[0]
            status, payload = dispatch("GET", path, self.headers, {}, ctx)
            self._respond(status, payload)

        def do_POST(self):
            if not self._rate_ok():
                return
            path = self.path.split("?")[0]
            body = self._read_body()
            if body is None:
                self._respond(413, {"error":
                                    f"request body missing, malformed, or larger "
                                    f"than {_MAX_BODY_BYTES} bytes"})
                return
            status, payload = dispatch("POST", path, self.headers, body, ctx)
            self._respond(status, payload)

        def log_message(self, *args):
            pass   # quiet by default

    return Handler


def make_server(host, port, ctx, rate_limit=120, rate_window=60):
    """Build the threaded API server. A per-client fixed-window rate limiter is
    on by default (``rate_limit`` requests per ``rate_window`` seconds); pass
    ``rate_limit=0`` to disable it."""
    limiter = RateLimiter(rate_limit, rate_window) if rate_limit else None
    return ThreadingHTTPServer((host, port), make_handler(ctx, limiter))
