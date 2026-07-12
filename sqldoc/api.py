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
import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqldoc import __version__
from sqldoc.adapters import get_adapter


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
    ("GET", "/api/agent/status"): _ep_agent_status,
}

# Endpoints that don't need a database connection.
_NO_ADAPTER = {("GET", "/api/agent/status")}


# --- dispatch (pure, socket-free — easy to test) ---------------------------

def dispatch(method, path, headers, body, ctx) -> tuple:
    """Return (status_code, dict). `headers` is a case-insensitive-ish mapping;
    `ctx` carries conn_str/dialect/database/api_key/mode/model."""
    path = path.rstrip("/") or "/api"
    api_key = ctx.get("api_key")
    if api_key:
        provided = headers.get("X-API-Key") or headers.get("x-api-key")
        if provided != api_key:
            return 401, {"error": "invalid or missing X-API-Key header"}

    if method == "GET" and path in ("/api", "/"):
        return 200, {"service": "sqldoc", "version": __version__,
                     "endpoints": [f"{m} {p}" for (m, p) in ENDPOINTS]}

    handler = ENDPOINTS.get((method, path))
    if handler is None:
        return 404, {"error": f"no endpoint for {method} {path}"}

    try:
        adapter = None
        if (method, path) not in _NO_ADAPTER:
            if not ctx.get("conn_str"):
                return 400, {"error": "the server has no database configured"}
            adapter = get_adapter(ctx["conn_str"], ctx.get("dialect"))
        return 200, handler(adapter, ctx, {}, body)
    except ValueError as e:
        return 400, {"error": str(e)}
    except Exception as e:
        return 500, {"error": f"{type(e).__name__}: {e}"}


# --- HTTP server -----------------------------------------------------------

def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status, payload):
            data = json.dumps(payload, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            path = self.path.split("?")[0]
            status, payload = dispatch("GET", path, self.headers, {}, ctx)
            self._respond(status, payload)

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._read_body()
            status, payload = dispatch("POST", path, self.headers, body, ctx)
            self._respond(status, payload)

        def log_message(self, *args):
            pass   # quiet by default

    return Handler


def make_server(host, port, ctx):
    return ThreadingHTTPServer((host, port), make_handler(ctx))
