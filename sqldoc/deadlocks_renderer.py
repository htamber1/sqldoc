"""HTML + JSON rendering for the `sqldoc deadlocks` report (with SVG graphs)."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.deadlocks import summarize


def _layout(event):
    """Compute an SVG wait-for graph layout for one deadlock event: process nodes
    in a row with bezier wait-for edges (waiter -> owner) arced above."""
    procs = event.processes
    n = len(procs)
    if n == 0:
        return None
    node_w, node_h, gap, top = 180, 80, 50, 120
    width = max(640, n * (node_w + gap) + gap)
    index = {}
    nodes = []
    for i, p in enumerate(procs):
        x = gap + i * (node_w + gap)
        index[p.id] = (x + node_w / 2, top)
        nodes.append({
            "x": x, "y": top, "w": node_w, "h": node_h, "cx": x + node_w / 2,
            "spid": p.spid or p.id, "login": p.login, "lock": p.lock_mode,
            "is_victim": p.is_victim,
        })
    edges = []
    for i, (waiter, owner) in enumerate(event.edges):
        if waiter in index and owner in index:
            wx, wy = index[waiter]
            ox, oy = index[owner]
            arc = 60 + (i % 3) * 26
            edges.append({"d": f"M {wx} {wy} C {wx} {wy - arc}, {ox} {oy - arc}, {ox} {oy}"})
    height = top + node_h + 40
    return {"width": width, "height": height, "nodes": nodes, "edges": edges}


DEADLOCK_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ target }} — Deadlock Analysis</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --bg: #0a0a0f; --card: #1e2530; --card-head: #171d26;
            --text: #e5e7eb; --text-strong: #f8fafc; --muted: #94a3b8; --faint: #64748b;
            --border: #2a3340; --border-strong: #3a4658;
            --red: #f87171; --amber: #fbbf24; --green: #34d399; --blue: #60a5fa; --violet: #c084fc;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; }
        ::-webkit-scrollbar { width: 11px; height: 11px; }
        ::-webkit-scrollbar-track { background: #0a0e18; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 6px; border: 2px solid #0a0e18; }
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(248,113,113,0.13), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--red), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1100px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 20px; text-align: center; }
        .stat-card .number { font-size: 1.9rem; font-weight: 800; }
        .stat-card .label { color: var(--muted); font-size: 0.72rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
        .c-red .number { color: var(--red); } .c-blue .number { color: var(--blue); } .c-amber .number { color: var(--amber); }
        .ai { background: linear-gradient(180deg, #202a38, var(--card)); border: 1px solid rgba(96,165,250,0.3); border-radius: 14px; padding: 20px 24px; margin-bottom: 24px; }
        .ai h3 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.07em; color: var(--blue); margin-bottom: 10px; }
        .ai .body { color: #dbe4ee; font-size: 0.9rem; line-height: 1.6; white-space: pre-wrap; }
        .event { background: var(--card); border: 1px solid var(--border); border-radius: 14px; margin-bottom: 20px; overflow: hidden; }
        .event .ehead { padding: 14px 20px; background: var(--card-head); border-bottom: 1px solid var(--border-strong); display: flex; gap: 12px; align-items: center; }
        .event .ehead .t { font-family: 'Consolas', monospace; color: var(--text-strong); font-weight: 700; }
        .event .ehead .kind { font-size: 0.68rem; padding: 2px 9px; border-radius: 5px; background: rgba(248,113,113,0.14); color: var(--red); font-weight: 700; }
        .graphwrap { padding: 18px; overflow-x: auto; text-align: center; }
        .plist { padding: 0 20px 16px; }
        table { width: 100%; border-collapse: collapse; }
        th { padding: 8px 12px; text-align: left; font-size: 0.68rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border); }
        td { padding: 8px 12px; font-size: 0.82rem; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        .mono { font-family: 'Consolas', monospace; }
        .sql { font-family: 'Consolas', monospace; font-size: 0.78rem; color: #cbd5e1; white-space: pre-wrap; word-break: break-word; max-width: 440px; }
        .vic { color: var(--red); font-weight: 700; }
        .notice { background: rgba(148,163,184,0.08); border: 1px solid var(--border); border-radius: 12px; padding: 20px; color: var(--muted); font-size: 0.9rem; line-height: 1.5; }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .footer { max-width: 1100px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Deadlock Analysis</span>
        <h1>{{ target }}</h1>
        <p>Generated on {{ generated_at }} &middot; {{ report.mechanism }} ({{ report.dialect }})</p>
    </div>
    <div class="container">
        {% if report.errors %}
        <div class="warn">{% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }}</div>{% endfor %}</div>
        {% endif %}

        <div class="stats">
            <div class="stat-card {{ 'c-red' if summary.total_count else 'c-green' }}"><div class="number">{{ summary.total_count }}</div><div class="label">Deadlocks recorded</div></div>
            <div class="stat-card c-blue"><div class="number">{{ summary.graph_events }}</div><div class="label">Deadlock graphs</div></div>
            {% if summary.current_blocking %}<div class="stat-card c-amber"><div class="number">{{ summary.current_blocking }}</div><div class="label">Current blocking</div></div>{% endif %}
        </div>

        {% if report.ai_explanation %}
        <div class="ai"><h3>AI analysis</h3><div class="body">{{ report.ai_explanation }}</div></div>
        {% endif %}

        {% if report.notes and not report.events %}
        <div class="notice">{% for n in report.notes %}{{ n }}<br>{% endfor %}</div>
        {% endif %}

        {% for item in events %}
        {% set ev = item.event %}
        {% set g = item.layout %}
        <div class="event">
            <div class="ehead">
                <span class="kind">{{ 'DEADLOCK' if ev.kind == 'graph' else 'BLOCKING' }}</span>
                <span class="t">{{ ev.timestamp or '(current)' }}</span>
                {% if ev.resources %}<span style="color: var(--muted); font-size: 0.8rem;">on {{ ev.resources|join(', ') }}</span>{% endif %}
            </div>
            {% if g %}
            <div class="graphwrap">
                <svg width="{{ g.width }}" height="{{ g.height }}" viewBox="0 0 {{ g.width }} {{ g.height }}" xmlns="http://www.w3.org/2000/svg">
                    <defs><marker id="dlarrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#f87171"/></marker></defs>
                    {% for e in g.edges %}<path d="{{ e.d }}" fill="none" stroke="#f87171" stroke-width="2" marker-end="url(#dlarrow)" opacity="0.85"/>{% endfor %}
                    {% for nd in g.nodes %}
                    <rect x="{{ nd.x }}" y="{{ nd.y }}" width="{{ nd.w }}" height="{{ nd.h }}" rx="10" fill="#1e2530" stroke="{{ '#f87171' if nd.is_victim else '#60a5fa' }}" stroke-width="{{ 3 if nd.is_victim else 2 }}"/>
                    <text x="{{ nd.cx }}" y="{{ nd.y + 28 }}" text-anchor="middle" fill="#f8fafc" font-size="13" font-weight="700" font-family="Consolas, monospace">spid {{ nd.spid }}</text>
                    <text x="{{ nd.cx }}" y="{{ nd.y + 47 }}" text-anchor="middle" fill="#94a3b8" font-size="11" font-family="Consolas, monospace">{{ nd.login }}</text>
                    <text x="{{ nd.cx }}" y="{{ nd.y + 65 }}" text-anchor="middle" fill="{{ '#f87171' if nd.is_victim else '#60a5fa' }}" font-size="10" font-family="Consolas, monospace">{{ 'VICTIM' if nd.is_victim else ('lock ' + nd.lock if nd.lock else '') }}</text>
                    {% endfor %}
                </svg>
            </div>
            {% endif %}
            <div class="plist">
                <table>
                    <thead><tr><th>SPID</th><th>Login</th><th>Lock</th><th>Wait resource</th><th>Statement</th></tr></thead>
                    <tbody>
                        {% for p in ev.processes %}
                        <tr>
                            <td class="mono {{ 'vic' if p.is_victim else '' }}">{{ p.spid }}{% if p.is_victim %} (victim){% endif %}</td>
                            <td class="mono">{{ p.login }}</td>
                            <td class="mono">{{ p.lock_mode or '—' }}</td>
                            <td class="mono">{{ p.wait_resource or '—' }}</td>
                            <td class="sql">{{ p.query or '—' }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        {% endfor %}
    </div>
    <div class="footer">
        <strong>About deadlock analysis.</strong> SQL Server deadlock graphs are parsed from the always-on <code>system_health</code>
        extended-events session (size-limited — older deadlocks roll off). PostgreSQL shows the cumulative <code>pg_stat_database</code>
        deadlock count plus current blocking; MySQL shows the ER_LOCK_DEADLOCK error count. The AI analysis (when enabled) receives the
        deadlock's SQL statements.
    </div>
</body>
</html>
"""


def build_deadlocks_json(target: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "deadlocks",
        "target": target,
        "dialect": report.dialect,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mechanism": report.mechanism,
        "total_count": report.total_count,
        "summary": summarize(report),
        "events": [{
            "kind": e.kind, "timestamp": e.timestamp, "victim_id": e.victim_id,
            "resources": e.resources, "edges": e.edges,
            "processes": [asdict(p) for p in e.processes],
        } for e in report.events],
        "ai_explanation": report.ai_explanation,
        "notes": report.notes,
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_deadlocks_html(target, report, output_path):
    events = [{"event": e, "layout": _layout(e)} for e in report.events]
    template = Environment(autoescape=True).from_string(DEADLOCK_TEMPLATE)
    html = template.render(
        target=target,
        report=report,
        summary=summarize(report),
        events=events,
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Deadlock report written to {output_path}")
