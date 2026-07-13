"""Local web dashboard for the agent (stdlib http.server, no dependencies).

Serves an always-current view of every monitored database: overview cards (PII
risk score, health issues, table count, last run), a per-database page with the
schema-change timeline and health/PII trend sparklines, and the full generated
documentation HTML. The HTML is produced by pure functions (`render_overview` /
`render_db_page`) so they can be unit-tested without a running server.
"""
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#0d1117; color:#c9d1d9; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
a { color:#58a6ff; text-decoration:none; } a:hover { text-decoration:underline; }
header { padding:18px 28px; border-bottom:1px solid #21262d; display:flex; align-items:center; gap:12px; }
header h1 { font-size:18px; margin:0; } header .sub { color:#8b949e; font-size:12px; }
.wrap { padding:24px 28px; max-width:1100px; margin:0 auto; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px; }
.card { background:#161b22; border:1px solid #21262d; border-radius:10px; padding:16px; }
.card h2 { margin:0 0 10px; font-size:15px; }
.metrics { display:flex; gap:18px; flex-wrap:wrap; margin:8px 0; }
.metric { }
.metric .n { font-size:22px; font-weight:600; } .metric .l { color:#8b949e; font-size:11px; text-transform:uppercase; }
.score { font-size:26px; font-weight:700; }
.s-low { color:#3fb950; } .s-med { color:#d29922; } .s-high { color:#f85149; }
.pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; border:1px solid #30363d; }
.timeline { list-style:none; padding:0; margin:0; }
.timeline li { padding:8px 0; border-bottom:1px solid #21262d; display:flex; gap:12px; }
.timeline .t { color:#8b949e; font-size:12px; white-space:nowrap; }
.tag { font-size:11px; padding:1px 6px; border-radius:4px; }
.tag.schema_change { background:#1f6feb33; color:#79c0ff; }
.tag.new_pii { background:#f8514933; color:#ffa198; }
.tag.health_degradation { background:#d2992233; color:#e3b341; }
.tag.error { background:#8b949e33; color:#c9d1d9; }
.muted { color:#8b949e; } .ok { color:#3fb950; } .err { color:#f85149; }
svg.spark { vertical-align:middle; }
"""


def _score_class(score):
    return "s-high" if score >= 40 else "s-med" if score >= 15 else "s-low"


def _sparkline(values, w=160, h=32, stroke="#58a6ff"):
    vals = [float(v or 0) for v in values]
    if len(vals) < 2:
        return f'<svg class="spark" width="{w}" height="{h}"></svg>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i*step:.1f},{h - (v-lo)/rng*(h-4) - 2:.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark" width="{w}" height="{h}">'
            f'<polyline fill="none" stroke="{stroke}" stroke-width="1.5" points="{pts}"/></svg>')


def _page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><header><h1>&#128203; sqldoc agent</h1>"
            f"<span class='sub'>live database monitoring</span></header>"
            f"<div class='wrap'>{body}</div></body></html>")


def render_overview(store) -> str:
    dbs = store.list_databases()
    if not dbs:
        return _page("sqldoc agent", "<p class='muted'>No databases monitored yet. "
                     "The agent records data after its first poll.</p>")
    cards = []
    for name in dbs:
        m = store.latest_metric(name) or {}
        run = store.last_run(name) or {}
        score = m.get("pii_score") or 0
        status = run.get("status", "—")
        status_html = (f"<span class='ok'>ok</span>" if status == "ok"
                       else f"<span class='err'>{html.escape(status)}</span>")
        cards.append(
            f"<div class='card'><h2><a href='/db/{html.escape(name)}'>{html.escape(name)}</a></h2>"
            f"<div class='score {_score_class(score)}'>{score:g}<span class='l' "
            f"style='font-size:11px'> PII risk</span></div>"
            f"<div class='metrics'>"
            f"<div class='metric'><div class='n'>{m.get('tables',0)}</div><div class='l'>tables</div></div>"
            f"<div class='metric'><div class='n'>{m.get('columns',0)}</div><div class='l'>columns</div></div>"
            f"<div class='metric'><div class='n'>{m.get('health_issues',0)}</div><div class='l'>health issues</div></div>"
            f"</div><div class='muted' style='font-size:12px'>last run: "
            f"{html.escape(str(run.get('finished_at') or run.get('started_at') or '—'))} · {status_html}</div></div>")
    nav = "<p><a href='/alerts'>&#128276; Alert history (30 days)</a></p>"
    return _page("sqldoc agent", nav + f"<div class='grid'>{''.join(cards)}</div>")


def render_db_page(store, name) -> str:
    if name not in store.list_databases():
        return _page("not found", f"<p class='err'>Unknown database '{html.escape(name)}'.</p>"
                     "<p><a href='/'>&larr; back</a></p>")
    m = store.latest_metric(name) or {}
    hist = store.metrics_history(name, limit=100)
    score = m.get("pii_score") or 0
    doc_html, doc_at = store.get_doc(name)

    trends = (
        "<div class='card'><h2>Trends</h2><div class='metrics'>"
        f"<div class='metric'><div class='l'>PII risk</div>{_sparkline([h['pii_score'] for h in hist], stroke='#f85149')}</div>"
        f"<div class='metric'><div class='l'>health issues</div>{_sparkline([h['health_issues'] for h in hist], stroke='#d29922')}</div>"
        f"<div class='metric'><div class='l'>columns</div>{_sparkline([h['columns'] for h in hist])}</div>"
        "</div></div>")

    events = store.recent_events(name, limit=40)
    if events:
        items = "".join(
            f"<li><span class='t'>{html.escape(e['at'])}</span>"
            f"<span class='tag {html.escape(e['type'])}'>{html.escape(e['type'])}</span>"
            f"<span>{html.escape(e['summary'])}</span></li>" for e in events)
        timeline = f"<div class='card'><h2>Change timeline</h2><ul class='timeline'>{items}</ul></div>"
    else:
        timeline = "<div class='card'><h2>Change timeline</h2><p class='muted'>No changes recorded yet.</p></div>"

    doc_link = (f"<p><a href='/db/{html.escape(name)}/doc'>&#128196; Open full documentation</a> "
                f"<span class='muted'>(updated {html.escape(str(doc_at))})</span></p>"
                if doc_html else "<p class='muted'>Documentation not generated yet.</p>")

    header = (f"<p><a href='/'>&larr; all databases</a></p>"
              f"<div class='card'><h2>{html.escape(name)}</h2>"
              f"<div class='score {_score_class(score)}'>{score:g}<span class='l'> PII risk score</span></div>"
              f"<div class='metrics'>"
              f"<div class='metric'><div class='n'>{m.get('pii_high',0)}</div><div class='l'>high</div></div>"
              f"<div class='metric'><div class='n'>{m.get('pii_medium',0)}</div><div class='l'>medium</div></div>"
              f"<div class='metric'><div class='n'>{m.get('pii_low',0)}</div><div class='l'>low</div></div>"
              f"<div class='metric'><div class='n'>{m.get('health_issues',0)}</div><div class='l'>health issues</div></div>"
              f"</div>{doc_link}</div>")
    return _page(f"{name} · sqldoc agent", header + trends + timeline)


_ALERT_STATUS_LABEL = {
    "fired": ("sent", "err"),
    "escalated": ("escalated", "err"),
    "suppressed_maintenance": ("suppressed (maintenance)", "muted"),
    "suppressed_dedup": ("suppressed (duplicate)", "muted"),
    "resolved": ("resolved", "ok"),
}


def render_alerts(store, days: int = 30) -> str:
    """30-day alert history: severity, status (sent / suppressed / escalated),
    and the channels that accepted each alert."""
    try:
        alerts = store.alerts_since_days(days)
    except Exception:
        alerts = []
    header = ("<p><a href='/'>&larr; all databases</a></p>"
              f"<div class='card'><h2>Alert history &middot; last {days} days</h2>")
    if not alerts:
        return _page("Alerts · sqldoc agent",
                     header + "<p class='muted'>No alerts recorded yet.</p></div>")
    rows = []
    for a in alerts:
        label, cls = _ALERT_STATUS_LABEL.get(a.get("status", ""), (a.get("status", ""), "muted"))
        chans = html.escape(a.get("channels") or "")
        rows.append(
            "<li>"
            f"<span class='t'>{html.escape(str(a.get('at')))}</span>"
            f"<span class='tag'>{html.escape(str(a.get('severity')))}</span>"
            f"<span class='tag {html.escape(str(a.get('type')))}'>{html.escape(str(a.get('type')))}</span>"
            f"<span>{html.escape(str(a.get('summary') or ''))}</span> "
            f"<span class='{cls}'>[{html.escape(label)}]</span>"
            + (f" <span class='muted' style='font-size:11px'>&rarr; {chans}</span>" if chans else "")
            + "</li>")
    counts = {}
    for a in alerts:
        counts[a.get("status")] = counts.get(a.get("status"), 0) + 1
    summary = " &middot; ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    body = (header + f"<p class='muted' style='font-size:12px'>{html.escape(summary)}</p>"
            f"<ul class='timeline'>{''.join(rows)}</ul></div>")
    return _page("Alerts · sqldoc agent", body)


def _handle_approval_link(raw_path: str) -> str:
    """Record an approve/reject decision from an emailed link (best-effort; the
    Jira rejection comment is posted when a decision is recorded via the CLI,
    which has the full config)."""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(raw_path).query)
    token = (q.get("token") or [""])[0]
    reason = (q.get("reason") or [None])[0]
    decision = "approve" if "/approve" in raw_path else "reject"
    from sqldoc.access import approval
    try:
        rec = approval.record_decision({}, token, decision, reason=reason)
    except ValueError as e:
        return _page("Approval", f"<p class='err'>{html.escape(str(e))}</p>")
    label = rec.get("status", decision)
    return _page("Approval recorded",
                 f"<div class='card'><h2>Decision recorded: {html.escape(label)}</h2>"
                 f"<p class='muted'>{html.escape(rec.get('database',''))} / "
                 f"{html.escape(rec.get('login',''))}</p></div>")


def alerts_json(store, days: int = 30) -> dict:
    try:
        return {"alerts": store.alerts_since_days(days)}
    except Exception:
        return {"alerts": []}


def overview_json(store) -> dict:
    out = {"databases": []}
    for name in store.list_databases():
        m = store.latest_metric(name) or {}
        run = store.last_run(name) or {}
        out["databases"].append({
            "name": name, "pii_score": m.get("pii_score"),
            "tables": m.get("tables"), "columns": m.get("columns"),
            "health_issues": m.get("health_issues"),
            "last_run": run.get("finished_at") or run.get("started_at"),
            "status": run.get("status"),
        })
    return out


# Security headers for the dashboard. The pages embed inline CSS + SVG, so the
# CSP allows 'self' + 'unsafe-inline' for styles/images but blocks framing,
# external script/connect, and object embeds. Responses hold monitoring data,
# so they are marked no-store and non-sniffable.
_DASH_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; script-src 'self' 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _make_handler(store, authn=None):
    class Handler(BaseHTTPRequestHandler):
        def _security_headers(self):
            for name, value in _DASH_SECURITY_HEADERS.items():
                self.send_header(name, value)

        def _send(self, body, content_type="text/html; charset=utf-8", code=200):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            """SSO gate: when auth is configured, require a valid OIDC bearer /
            SAML assertion. Returns True when allowed, else sends 401."""
            if authn is None or not getattr(authn, "enabled", False):
                return True
            ok, result = authn.authenticate(self.headers)
            if ok:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._security_headers()
            self.end_headers()
            msg = html.escape(str(result))
            self.wfile.write(_page("Sign in required",
                                   f"<p class='err'>Authentication required: {msg}</p>"
                                   "<p class='muted'>Provide an OIDC bearer token "
                                   "(Authorization: Bearer) or a SAML assertion.</p>").encode("utf-8"))
            return False

        def do_GET(self):
            if not self._authorized():
                return
            path = unquote(self.path.split("?", 1)[0]).rstrip("/") or "/"
            try:
                if path == "/":
                    self._send(render_overview(store))
                elif path == "/api/overview":
                    self._send(json.dumps(overview_json(store), indent=2),
                               "application/json; charset=utf-8")
                elif path in ("/access/approve", "/access/reject"):
                    self._send(_handle_approval_link(self.path))
                elif path == "/alerts":
                    self._send(render_alerts(store))
                elif path == "/api/alerts":
                    self._send(json.dumps(alerts_json(store), indent=2),
                               "application/json; charset=utf-8")
                elif path.startswith("/db/") and path.endswith("/doc"):
                    name = path[len("/db/"):-len("/doc")]
                    doc, _ = store.get_doc(name)
                    if doc:
                        self._send(doc)
                    else:
                        self._send(_page("no doc", "<p class='muted'>No documentation yet.</p>"), code=404)
                elif path.startswith("/db/"):
                    self._send(render_db_page(store, path[len("/db/"):]))
                else:
                    self._send(_page("404", "<p class='err'>Not found.</p>"), code=404)
            except Exception as e:  # never let a request crash the daemon
                self._send(_page("error", f"<p class='err'>{html.escape(str(e))}</p>"), code=500)

        def log_message(self, *args):
            pass   # keep the agent log clean

    return Handler


def make_server(store, port: int, host: str = "127.0.0.1", authn=None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _make_handler(store, authn))
