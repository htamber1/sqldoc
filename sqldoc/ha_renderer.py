"""HTML + JSON rendering for the `sqldoc ha` report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.ha import summarize

HA_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ target }} — High Availability</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(52,211,153,0.12), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--green), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1100px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 26px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 20px; text-align: center; }
        .stat-card .number { font-size: 1.9rem; font-weight: 800; }
        .stat-card .label { color: var(--muted); font-size: 0.72rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); }
        .c-green .number { color: var(--green); } .c-blue .number { color: var(--blue); }
        .topo { display: flex; align-items: center; justify-content: center; gap: 0; flex-wrap: wrap; margin: 10px 0 26px; }
        .node { min-width: 150px; text-align: center; padding: 16px 18px; border-radius: 14px; border: 2px solid var(--border); background: var(--card); margin: 8px; }
        .node.primary { border-color: var(--blue); }
        .node.healthy { border-color: var(--green); }
        .node.unhealthy { border-color: var(--red); }
        .node .role { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
        .node .srv { font-family: 'Consolas', monospace; font-weight: 700; color: var(--text-strong); margin: 4px 0; }
        .node .sub { font-size: 0.76rem; color: var(--muted); }
        .arrow { color: var(--muted); font-size: 1.4rem; }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 20px 0 12px; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 16px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 10px 16px; font-size: 0.85rem; border-bottom: 1px solid var(--border); }
        tr:last-child td { border-bottom: none; }
        .mono { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .num { text-align: right; font-family: 'Consolas', monospace; }
        .pill { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; }
        .pill.ok { background: rgba(52,211,153,0.15); color: var(--green); }
        .pill.bad { background: rgba(220,38,38,0.15); color: var(--red); }
        .pill.role { background: rgba(96,165,250,0.15); color: var(--blue); }
        .notice { background: rgba(148,163,184,0.08); border: 1px solid var(--border); border-radius: 12px; padding: 22px; text-align: center; color: var(--muted); font-size: 0.95rem; }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .footer { max-width: 1100px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; High Availability</span>
        <h1>{{ target }}</h1>
        <p>Generated on {{ generated_at }} &middot; {{ report.mechanism }} ({{ report.dialect }})</p>
    </div>
    <div class="container">
        {% if report.errors %}
        <div class="warn">{% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }}</div>{% endfor %}</div>
        {% endif %}

        {% if not report.ha_enabled %}
        <div class="notice">{% for n in report.notes %}{{ n }}<br>{% endfor %}</div>
        {% else %}
        <div class="stats">
            <div class="stat-card c-blue"><div class="number">{{ summary.replicas }}</div><div class="label">Replicas</div></div>
            <div class="stat-card {{ 'c-red' if summary.unhealthy else 'c-green' }}"><div class="number">{{ summary.unhealthy }}</div><div class="label">Unhealthy</div></div>
            <div class="stat-card c-amber"><div class="number">{% if summary.max_lag_seconds is not none %}{{ summary.max_lag_seconds }}s{% else %}—{% endif %}</div><div class="label">Max lag</div></div>
        </div>

        <div class="topo">
            {% for r in report.replicas %}
            {% if not loop.first %}<span class="arrow">&rarr;</span>{% endif %}
            <div class="node {{ 'primary' if r.role|upper in ['PRIMARY','SOURCE'] else ('unhealthy' if not r.is_healthy else 'healthy') }}">
                <div class="role">{{ r.role }}</div>
                <div class="srv">{{ r.server or '(local)' }}</div>
                <div class="sub">{{ r.sync_state or r.state }}{% if r.lag_seconds is not none %} &middot; {{ r.lag_seconds }}s behind{% endif %}</div>
            </div>
            {% endfor %}
        </div>

        <h2 class="section">Replicas</h2>
        <div class="panel">
            <table>
                <thead><tr><th>Server</th><th>Role</th><th>Sync state</th><th>Health</th><th>Lag (s)</th><th>Lag (bytes)</th></tr></thead>
                <tbody>
                    {% for r in report.replicas %}
                    <tr>
                        <td class="mono">{{ r.server or '(local)' }}{% if r.ag_name %} <span class="sub">/{{ r.ag_name }}</span>{% endif %}</td>
                        <td><span class="pill role">{{ r.role }}</span></td>
                        <td>{{ r.sync_state or r.state or '—' }}{% if r.io_running %} <span class="sub">(IO {{ r.io_running }}, SQL {{ r.sql_running }})</span>{% endif %}</td>
                        <td><span class="pill {{ 'ok' if r.is_healthy else 'bad' }}">{{ 'healthy' if r.is_healthy else 'unhealthy' }}</span></td>
                        <td class="num">{% if r.lag_seconds is not none %}{{ r.lag_seconds }}{% else %}—{% endif %}</td>
                        <td class="num">{% if r.lag_bytes is not none %}{{ '{:,}'.format(r.lag_bytes) }}{% else %}—{% endif %}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
    </div>
    <div class="footer">
        <strong>About HA monitoring.</strong> Replica state comes from the engine's replication catalog/DMVs
        (<code>sys.dm_hadr_*</code> / <code>pg_stat_replication</code> / <code>SHOW REPLICA STATUS</code>). SQL Server lag is shown as
        send+redo queue bytes; PostgreSQL/MySQL report seconds behind. No table row data was read.
    </div>
</body>
</html>
"""


def build_ha_json(target: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "ha",
        "target": target,
        "dialect": report.dialect,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ha_enabled": report.ha_enabled,
        "mechanism": report.mechanism,
        "summary": summarize(report),
        "replicas": [{**asdict(r), "is_healthy": r.is_healthy} for r in report.replicas],
        "notes": report.notes,
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_ha_html(target, report, output_path):
    template = Environment(autoescape=True).from_string(HA_TEMPLATE)
    html = template.render(
        target=target,
        report=report,
        summary=summarize(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"High-availability report written to {output_path}")
