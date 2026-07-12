"""HTML + JSON rendering for the `sqldoc intel` report."""
import math
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.intel import summarize, summarize_linked


def _topology(report):
    """Node coordinates for the linked-server star diagram (local instance in the
    centre, each linked server on a surrounding circle)."""
    ls = getattr(report, "linked_servers", None)
    if not ls or not ls.linked_servers:
        return None
    servers = ls.linked_servers
    n = len(servers)
    cx, cy, radius = 450, 190, 140
    nodes = []
    for i, s in enumerate(servers):
        ang = (2 * math.pi * i / n) - math.pi / 2 if n > 1 else -math.pi / 2
        nodes.append({
            "x": round(cx + radius * math.cos(ang), 1),
            "y": round(cy + radius * math.sin(ang), 1),
            "name": s.name,
            "reachable": s.reachable,
        })
    return {"cx": cx, "cy": cy, "width": 900, "height": 380,
            "center": ls.local_server or "(local)", "nodes": nodes}

INTEL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ database }} — Schema Intelligence</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(192,132,252,0.13), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--violet), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1200px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 22px; text-align: center; }
        .stat-card .number { font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.78rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.07em; }
        .c-amber .number { color: var(--amber); } .c-red .number { color: var(--red); }
        .c-blue .number { color: var(--blue); } .c-violet .number { color: var(--violet); }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 30px 0 12px; display: flex; align-items: center; gap: 10px; }
        h2.section .n { font-size: 0.8rem; color: var(--muted); font-weight: 600; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 16px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 11px 16px; font-size: 0.85rem; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255,255,255,0.025); }
        .loc { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .kind { display: inline-block; padding: 2px 9px; border-radius: 5px; font-size: 0.7rem; font-weight: 700; background: rgba(96,165,250,0.14); color: var(--blue); border: 1px solid rgba(96,165,250,0.3); }
        .dep { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.72rem; margin: 1px 3px 1px 0; background: rgba(255,255,255,0.05); color: #cbd5e1; border: 1px solid var(--border); font-family: 'Consolas', monospace; }
        .muted { color: var(--muted); font-size: 0.82rem; }
        .mono { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .lspill { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.66rem; font-weight: 700; margin: 1px 2px; background: rgba(148,163,184,0.12); color: var(--muted); border: 1px solid var(--border); }
        .lspill.rpc { background: rgba(96,165,250,0.14); color: var(--blue); border-color: rgba(96,165,250,0.3); }
        .lspill.da { background: rgba(251,146,60,0.14); color: #fb923c; border-color: rgba(251,146,60,0.3); }
        .lspill.rl { background: rgba(245,158,11,0.14); color: var(--amber); border-color: rgba(245,158,11,0.35); }
        .lspill.ok { background: rgba(52,211,153,0.14); color: var(--green); border-color: rgba(52,211,153,0.35); }
        .lspill.bad { background: rgba(220,38,38,0.15); color: var(--red); border-color: rgba(220,38,38,0.4); }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        pre.sql { margin: 0; padding: 18px 20px; background: #0c1119; color: #cbd5e1; font-family: 'Consolas', monospace; font-size: 0.8rem; line-height: 1.55; white-space: pre; overflow-x: auto; }
        .footer { max-width: 1200px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Schema Intelligence</span>
        <h1>{{ database }}</h1>
        <p>Generated on {{ generated_at }} &middot; naming, orphaned FKs, impact analysis{{ ', migration' if report.migration_sql else '' }}</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-amber"><div class="number">{{ summary.naming_issues }}</div><div class="label">Naming issues</div></div>
            <div class="stat-card c-red"><div class="number">{{ summary.orphan_fks }}</div><div class="label">Orphaned FKs</div></div>
            <div class="stat-card c-violet"><div class="number">{{ summary.high_impact_tables }}</div><div class="label">High-impact tables</div></div>
            <div class="stat-card c-blue"><div class="number">{{ report.impacts|length }}</div><div class="label">Tables analyzed</div></div>
            {% if summary.linked_servers %}<div class="stat-card {{ 'c-red' if summary.unreachable_linked_servers else 'c-green' }}"><div class="number">{{ summary.linked_servers }}</div><div class="label">Linked servers</div></div>{% endif %}
        </div>

        <h2 class="section">Naming conventions <span class="n">outliers vs the dominant style</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Kind</th><th>Identifier</th><th>Detail</th><th>Suggestion</th></tr></thead>
                <tbody>
                    {% for n in report.naming_issues %}
                    <tr>
                        <td><span class="kind">{{ n.kind }}</span></td>
                        <td class="loc">{{ n.object }}</td>
                        <td class="muted">{{ n.detail }}</td>
                        <td class="muted">{{ n.suggestion }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.naming_issues %}<div class="empty">Naming looks consistent.</div>{% endif %}
        </div>

        <h2 class="section">Orphaned foreign keys <span class="n">implied relationships without a constraint</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Column</th><th>Implied table</th><th>Detail</th></tr></thead>
                <tbody>
                    {% for o in report.orphan_fks %}
                    <tr>
                        <td class="loc">{{ o.schema }}.{{ o.table }}.{{ o.column }}</td>
                        <td class="loc">{{ o.implied_table }}</td>
                        <td class="muted">{{ o.detail }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.orphan_fks %}<div class="empty">No orphaned foreign keys detected.</div>{% endif %}
        </div>

        <h2 class="section">Impact analysis <span class="n">what breaks if you drop a table</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Table</th><th>Deps</th><th>FKs → it</th><th>Views / procs / triggers</th></tr></thead>
                <tbody>
                    {% for i in report.impacts if i.total > 0 %}
                    <tr>
                        <td class="loc">{{ i.schema }}.{{ i.table }}</td>
                        <td class="loc">{{ i.total }}</td>
                        <td>{% for d in i.fk_dependents %}<span class="dep">{{ d }}</span>{% endfor %}{% if not i.fk_dependents %}<span class="muted">—</span>{% endif %}</td>
                        <td>
                            {% for d in i.view_dependents %}<span class="dep">{{ d }}</span>{% endfor %}
                            {% for d in i.proc_dependents %}<span class="dep">{{ d }}</span>{% endfor %}
                            {% for d in i.trigger_dependents %}<span class="dep">{{ d }}</span>{% endfor %}
                            {% if not (i.view_dependents or i.proc_dependents or i.trigger_dependents) %}<span class="muted">—</span>{% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if report.impacts|selectattr('total')|list|length == 0 %}<div class="empty">No inbound dependencies detected.</div>{% endif %}
        </div>

        {% if report.linked_servers and report.linked_servers.linked_servers %}
        <h2 class="section">Linked servers <span class="n">network topology, security config &amp; connectivity</span></h2>
        {% if topology %}
        <div class="panel" style="margin-bottom:16px;"><div style="padding:18px; overflow-x:auto; text-align:center;">
            <svg width="{{ topology.width }}" height="{{ topology.height }}" viewBox="0 0 {{ topology.width }} {{ topology.height }}" xmlns="http://www.w3.org/2000/svg">
                {% for nd in topology.nodes %}
                <line x1="{{ topology.cx }}" y1="{{ topology.cy }}" x2="{{ nd.x }}" y2="{{ nd.y }}" stroke="{{ '#34d399' if nd.reachable else ('#f87171' if nd.reachable == false else '#64748b') }}" stroke-width="2" opacity="0.7"/>
                {% endfor %}
                {% for nd in topology.nodes %}
                <circle cx="{{ nd.x }}" cy="{{ nd.y }}" r="24" fill="#1e2530" stroke="{{ '#34d399' if nd.reachable else ('#f87171' if nd.reachable == false else '#64748b') }}" stroke-width="2"/>
                <text x="{{ nd.x }}" y="{{ nd.y + 42 }}" text-anchor="middle" fill="#cbd5e1" font-size="12" font-family="Consolas, monospace">{{ nd.name }}</text>
                {% endfor %}
                <circle cx="{{ topology.cx }}" cy="{{ topology.cy }}" r="32" fill="#171d26" stroke="#60a5fa" stroke-width="3"/>
                <text x="{{ topology.cx }}" y="{{ topology.cy + 5 }}" text-anchor="middle" fill="#f8fafc" font-size="12" font-weight="700" font-family="Consolas, monospace">{{ topology.center }}</text>
            </svg>
        </div></div>
        {% endif %}
        <div class="panel">
            <table>
                <thead><tr><th>Linked server</th><th>Product</th><th>Data source</th><th>Security</th><th>Login mappings</th><th>Connectivity</th></tr></thead>
                <tbody>
                    {% for ls in report.linked_servers.linked_servers %}
                    <tr>
                        <td class="mono">{{ ls.name }}{% if ls.catalog %} <span class="muted">/{{ ls.catalog }}</span>{% endif %}</td>
                        <td>{{ ls.product }}<div class="muted" style="font-size:0.72rem;">{{ ls.provider }}</div></td>
                        <td class="mono">{{ ls.data_source }}</td>
                        <td>
                            {% if ls.is_rpc_out %}<span class="lspill rpc">RPC out</span>{% endif %}
                            {% if ls.is_data_access %}<span class="lspill da">data access</span>{% endif %}
                            {% if ls.is_remote_login %}<span class="lspill rl">remote login</span>{% endif %}
                        </td>
                        <td>{% for lg in ls.logins %}<div class="mono" style="font-size:0.75rem;">{{ lg.local_login }} &rarr; {{ lg.remote_login }}{% if lg.uses_self %} <span class="muted">(self)</span>{% endif %}</div>{% endfor %}{% if not ls.logins %}<span class="muted">—</span>{% endif %}</td>
                        <td>
                            {% if ls.reachable %}<span class="lspill ok">reachable</span>{% elif ls.reachable == false %}<span class="lspill bad">unreachable</span>{% else %}<span class="lspill">not tested</span>{% endif %}
                            {% if ls.remote_version %}<div class="muted" style="font-size:0.72rem;">v{{ ls.remote_version }} {{ ls.remote_edition }}</div>{% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}

        {% if report.migration_sql %}
        <h2 class="section">Migration script <span class="n">baseline → current (review before running)</span></h2>
        <div class="panel"><pre class="sql">{{ report.migration_sql }}</pre></div>
        {% endif %}
    </div>
    <div class="footer">
        <strong>Heuristic analysis.</strong> Naming/orphan-FK findings are suggestions inferred from identifiers — review before renaming or
        adding constraints. Impact analysis matches table names in view/procedure/trigger SQL and the FK graph; dynamic SQL can hide
        dependencies. Generated migrations use snapshot types (no length/precision) and are a starting point, not a drop-in script.
    </div>
</body>
</html>
"""


def build_intel_json(database: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "intel",
        "database": database,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(report),
        "naming_issues": [asdict(n) for n in report.naming_issues],
        "orphan_fks": [asdict(o) for o in report.orphan_fks],
        "impacts": [{**asdict(i), "total": i.total}
                    for i in report.impacts if i.total > 0],
        "migration_sql": report.migration_sql,
        "linked_servers": (None if not report.linked_servers else {
            "local_server": report.linked_servers.local_server,
            "traversed": report.linked_servers.traversed,
            "summary": summarize_linked(report.linked_servers),
            "servers": [asdict(s) for s in report.linked_servers.linked_servers],
            "errors": [{"section": a, "message": b} for a, b in report.linked_servers.errors],
        }),
    }


def render_intel_html(database, report, output_path):
    report.database = database
    template = Environment(autoescape=True).from_string(INTEL_TEMPLATE)
    html = template.render(
        database=database,
        report=report,
        summary=summarize(report),
        topology=_topology(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Schema-intelligence report written to {output_path}")
