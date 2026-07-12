"""HTML + JSON rendering for the `sqldoc comply` report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.comply import summarize

COMPLY_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ database }} — Compliance</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --bg: #0a0a0f; --card: #1e2530; --card-head: #171d26;
            --text: #e5e7eb; --text-strong: #f8fafc; --muted: #94a3b8; --faint: #64748b;
            --border: #2a3340; --border-strong: #3a4658;
            --red: #f87171; --amber: #fbbf24; --green: #34d399; --blue: #60a5fa; --violet: #c084fc;
            --high-bg: rgba(220,38,38,0.15); --high-bd: rgba(220,38,38,0.4);
            --med-bg: rgba(245,158,11,0.15); --med-bd: rgba(245,158,11,0.4);
            --low-bg: rgba(148,163,184,0.12); --low-bd: rgba(148,163,184,0.3);
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
        .container { max-width: 1200px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 22px; text-align: center; }
        .stat-card .number { font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.74rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.06em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); }
        .c-blue .number { color: var(--blue); } .c-green .number { color: var(--green); } .c-violet .number { color: var(--violet); }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 30px 0 12px; display: flex; align-items: center; gap: 10px; }
        h2.section .n { font-size: 0.8rem; color: var(--muted); font-weight: 600; }
        .reg { background: var(--card); border: 1px solid var(--border); border-radius: 14px; margin-bottom: 18px; overflow: hidden; }
        .reg-head { display: flex; align-items: center; gap: 14px; padding: 16px 20px; background: var(--card-head); border-bottom: 1px solid var(--border-strong); }
        .reg-head .name { font-size: 1.05rem; font-weight: 800; color: var(--text-strong); }
        .reg-head .counts { color: var(--muted); font-size: 0.82rem; }
        .reg-head .pill { margin-left: auto; padding: 3px 12px; border-radius: 20px; font-size: 0.72rem; font-weight: 700; }
        .pill.inscope { background: var(--high-bg); color: var(--red); border: 1px solid var(--high-bd); }
        .pill.clear { background: rgba(52,211,153,0.14); color: var(--green); border: 1px solid rgba(52,211,153,0.35); }
        .reg-body { display: grid; grid-template-columns: 1.1fr 1fr; gap: 0; }
        @media (max-width: 820px) { .reg-body { grid-template-columns: 1fr; } }
        .reg-body .col { padding: 16px 20px; }
        .reg-body .col + .col { border-left: 1px solid var(--border); }
        .subhead { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); font-weight: 700; margin-bottom: 10px; }
        .chip { display: inline-block; padding: 3px 10px; border-radius: 6px; font-size: 0.76rem; margin: 2px 4px 2px 0; font-family: 'Consolas', monospace; background: rgba(255,255,255,0.05); border: 1px solid var(--border); }
        .chip.HIGH { background: var(--high-bg); color: var(--red); border-color: var(--high-bd); }
        .chip.MEDIUM { background: var(--med-bg); color: var(--amber); border-color: var(--med-bd); }
        .chip.LOW { background: var(--low-bg); color: var(--muted); border-color: var(--low-bd); }
        ul.controls { list-style: none; }
        ul.controls li { position: relative; padding: 4px 0 4px 18px; font-size: 0.83rem; color: #cbd5e1; line-height: 1.45; }
        ul.controls li::before { content: "\\2713"; position: absolute; left: 0; color: var(--green); font-weight: 700; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 16px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 10px 16px; font-size: 0.85rem; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255,255,255,0.025); }
        .loc { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .via { font-family: 'Consolas', monospace; color: var(--blue); }
        .arrow { color: var(--muted); padding: 0 6px; }
        .kind { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.68rem; font-weight: 700; background: rgba(96,165,250,0.14); color: var(--blue); border: 1px solid rgba(96,165,250,0.3); }
        .risk { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; }
        .risk.HIGH { background: var(--high-bg); color: var(--red); border: 1px solid var(--high-bd); }
        .risk.MEDIUM { background: var(--med-bg); color: var(--amber); border: 1px solid var(--med-bd); }
        .risk.LOW { background: var(--low-bg); color: var(--muted); border: 1px solid var(--low-bd); }
        .level { display: inline-block; padding: 2px 9px; border-radius: 5px; font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; margin-right: 4px; }
        .level.read { background: rgba(96,165,250,0.14); color: var(--blue); border: 1px solid rgba(96,165,250,0.3); }
        .level.write { background: var(--med-bg); color: var(--amber); border: 1px solid var(--med-bd); }
        .level.admin { background: var(--high-bg); color: var(--red); border: 1px solid var(--high-bd); }
        .ptype { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.66rem; font-weight: 700; background: rgba(192,132,252,0.14); color: var(--violet); border: 1px solid rgba(192,132,252,0.3); margin-left: 6px; }
        details.roleexp { margin-top: 6px; }
        details.roleexp > summary { cursor: pointer; color: var(--blue); font-size: 0.78rem; list-style: none; user-select: none; }
        details.roleexp > summary::-webkit-details-marker { display: none; }
        details.roleexp > summary::before { content: "\\25b8 "; color: var(--muted); }
        details.roleexp[open] > summary::before { content: "\\25be "; }
        details.roleexp .members { margin-top: 6px; padding-left: 14px; }
        details.roleexp .members .m { display: inline-block; padding: 2px 9px; border-radius: 6px; font-size: 0.75rem; font-family: 'Consolas', monospace; margin: 2px 4px 2px 0; background: rgba(255,255,255,0.05); border: 1px solid var(--border); }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .footer { max-width: 1200px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Compliance</span>
        <h1>{{ database }}</h1>
        <p>Generated on {{ generated_at }} &middot; HIPAA / GDPR / PCI-DSS scope, data lineage, access audit</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-red"><div class="number">{{ summary.hipaa }}</div><div class="label">HIPAA columns</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.gdpr }}</div><div class="label">GDPR columns</div></div>
            <div class="stat-card c-blue"><div class="number">{{ summary.pci_dss }}</div><div class="label">PCI-DSS columns</div></div>
            <div class="stat-card c-green"><div class="number">{{ summary.access_alerts }}</div><div class="label">Access alerts</div></div>
            <div class="stat-card c-violet"><div class="number">{{ summary.principals }}</div><div class="label">Principals ({{ summary.roles }} roles)</div></div>
        </div>

        <h2 class="section">Regulatory scope <span class="n">what each regime covers + required controls</span></h2>
        {% for sec in report.regulations %}
        <div class="reg">
            <div class="reg-head">
                <span class="name">{{ sec.regulation }}</span>
                <span class="counts">{{ sec.column_count }} column(s) across {{ sec.table_count }} table(s){% if sec.high_count %} · {{ sec.high_count }} HIGH{% endif %}</span>
                <span class="pill {{ 'inscope' if sec.findings else 'clear' }}">{{ 'IN SCOPE' if sec.findings else 'NO FINDINGS' }}</span>
            </div>
            <div class="reg-body">
                <div class="col">
                    <div class="subhead">Regulated columns</div>
                    {% for f in sec.findings %}<span class="chip {{ f.risk }}">{{ f.schema }}.{{ f.table }}.{{ f.column }}</span>{% endfor %}
                    {% if not sec.findings %}<span class="muted">None detected.</span>{% endif %}
                </div>
                <div class="col">
                    <div class="subhead">Required controls</div>
                    <ul class="controls">{% for c in sec.controls %}<li>{{ c }}</li>{% endfor %}</ul>
                </div>
            </div>
        </div>
        {% endfor %}

        <h2 class="section">Data lineage <span class="n">how data flows through views &amp; procedures</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Source</th><th></th><th>Target</th><th>Via</th><th>Kind</th></tr></thead>
                <tbody>
                    {% for fl in report.lineage %}
                    <tr>
                        <td class="loc">{{ fl.source }}</td>
                        <td class="arrow">&rarr;</td>
                        <td class="loc">{{ fl.target }}</td>
                        <td class="via">{{ fl.via }}</td>
                        <td><span class="kind">{{ fl.kind }}</span></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.lineage %}<div class="empty">No data flows detected in view/procedure definitions.</div>{% endif %}
        </div>

        <h2 class="section">Access by principal <span class="n">unified view per user/role across all objects</span></h2>
        {% if report.role_members %}
        <p style="color: var(--muted); font-size: 0.82rem; margin-bottom: 12px;">{{ report.role_members|length }} role membership(s) resolved &mdash; expand a role to see its members.</p>
        {% endif %}
        <div class="panel">
            <table>
                <thead><tr><th>Principal</th><th>Access level</th><th>Objects</th><th>PII objects</th><th>Max risk</th><th>Regulations</th></tr></thead>
                <tbody>
                    {% for pa in report.principals %}
                    <tr>
                        <td class="loc">{{ pa.principal }}{% if pa.is_role %}<span class="ptype">ROLE</span>{% elif pa.principal_type %}<span class="ptype">{{ pa.principal_type }}</span>{% endif %}
                            {% if pa.is_role %}
                            <details class="roleexp">
                                <summary>{{ pa.members|length }} member(s)</summary>
                                <div class="members">{% for m in pa.members %}<span class="m">{{ m }}</span>{% endfor %}{% if not pa.members %}<span class="muted">No members.</span>{% endif %}</div>
                            </details>
                            {% endif %}
                        </td>
                        <td>{% for lv in pa.levels %}<span class="level {{ lv }}">{{ lv }}</span>{% endfor %}{% if not pa.levels %}<span class="muted">&mdash;</span>{% endif %}</td>
                        <td>{{ pa.object_count }}</td>
                        <td>{% if pa.pii_object_count %}<span class="risk {{ pa.max_risk }}">{{ pa.pii_object_count }}</span>{% else %}<span class="muted">0</span>{% endif %}</td>
                        <td>{% if pa.pii_object_count %}<span class="risk {{ pa.max_risk }}">{{ pa.max_risk }}</span>{% else %}<span class="muted">&mdash;</span>{% endif %}</td>
                        <td class="muted">{{ pa.regulations|join(', ') if pa.regulations else '—' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.principals %}<div class="empty">No principals with object grants{{ ' (or permissions could not be read)' if report.errors else '' }}.</div>{% endif %}
        </div>

        <h2 class="section">Access audit <span class="n">grants on tables holding regulated data</span></h2>
        {% if report.errors %}
        <div class="warn">
            {% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }} (needs VIEW DEFINITION / db access).</div>{% endfor %}
        </div>
        {% endif %}
        <div class="panel">
            <table>
                <thead><tr><th>Principal</th><th>Permission</th><th>Table</th><th>Max risk</th><th>Categories</th><th>Regulations</th></tr></thead>
                <tbody>
                    {% for a in report.access_alerts %}
                    <tr>
                        <td class="loc">{{ a.principal }}</td>
                        <td><span class="kind">{{ a.permission }}</span></td>
                        <td class="loc">{{ a.schema }}.{{ a.table }}</td>
                        <td><span class="risk {{ a.max_risk }}">{{ a.max_risk }}</span></td>
                        <td class="muted">{{ a.categories|join(', ') }}</td>
                        <td class="muted">{{ a.regulations|join(', ') }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.access_alerts %}<div class="empty">No grants on regulated tables{{ ' (or permissions could not be read)' if report.errors else '' }}.</div>{% endif %}
        </div>
    </div>
    <div class="footer">
        <strong>Scope &amp; method.</strong> Regulated columns come from the name/type PII scanner — heuristic, not a legal determination.
        Control lists are representative starting points, not exhaustive compliance requirements. Lineage is inferred by matching table
        names in view/procedure SQL (dynamic SQL can hide flows). Access alerts are object-level GRANTs from
        <code>sys.database_permissions</code> (SQL Server) / <code>information_schema.table_privileges</code> (PostgreSQL/MySQL). The
        per-principal view aggregates every grant a user/role holds and buckets it into read/write/admin; database role membership is
        expanded from <code>sys.database_role_members</code> (SQL Server) / <code>pg_auth_members</code> (PostgreSQL). Server-level rights
        are not resolved here. No table row data was read.
    </div>
</body>
</html>
"""


def build_comply_json(database: str, report) -> dict:
    def finding_row(f):
        return {"schema": f.schema, "table": f.table, "column": f.column,
                "category": f.category, "risk": f.risk, "regulations": f.regulations}
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "compliance",
        "database": database,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(report),
        "regulations": [{
            "regulation": s.regulation,
            "table_count": s.table_count,
            "column_count": s.column_count,
            "high_count": s.high_count,
            "controls": s.controls,
            "findings": [finding_row(f) for f in s.findings],
        } for s in report.regulations],
        "lineage": [asdict(fl) for fl in report.lineage],
        "permissions": [asdict(p) for p in report.permissions],
        "access_alerts": [asdict(a) for a in report.access_alerts],
        "role_members": [asdict(rm) for rm in report.role_members],
        "principals": [asdict(pa) for pa in report.principals],
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_comply_html(database, report, output_path):
    report.database = database
    template = Environment(autoescape=True).from_string(COMPLY_TEMPLATE)
    html = template.render(
        database=database,
        report=report,
        summary=summarize(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Compliance report written to {output_path}")
