"""HTML + JSON rendering for the multi-database board-level access report."""
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.comply_multi import summarize_multi

MULTI_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cross-Database Access — Compliance</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(192,132,252,0.13), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--violet), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1400px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 22px; text-align: center; }
        .stat-card .number { font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.72rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); }
        .c-blue .number { color: var(--blue); } .c-green .number { color: var(--green); } .c-violet .number { color: var(--violet); }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 30px 0 12px; display: flex; align-items: center; gap: 10px; }
        h2.section .n { font-size: 0.8rem; color: var(--muted); font-weight: 600; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 14px; text-align: left; font-size: 0.7rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        th.dbcol { text-align: center; color: var(--blue); }
        td { padding: 10px 14px; font-size: 0.84rem; border-bottom: 1px solid var(--border); vertical-align: middle; }
        td.dbcell { text-align: center; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255,255,255,0.025); }
        .princ { font-family: 'Consolas', monospace; color: var(--text-strong); font-weight: 600; }
        .ptype { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.62rem; font-weight: 700; background: rgba(192,132,252,0.14); color: var(--violet); border: 1px solid rgba(192,132,252,0.3); margin-left: 6px; }
        .level { display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 0.62rem; font-weight: 700; text-transform: uppercase; margin: 1px; }
        .level.read { background: rgba(96,165,250,0.14); color: var(--blue); }
        .level.write { background: var(--med-bg); color: var(--amber); }
        .level.admin { background: var(--high-bg); color: var(--red); }
        .none { color: var(--faint); }
        .risk { display: inline-block; padding: 3px 9px; border-radius: 20px; font-size: 0.68rem; font-weight: 700; }
        .risk.HIGH { background: var(--high-bg); color: var(--red); border: 1px solid var(--high-bd); }
        .risk.MEDIUM { background: var(--med-bg); color: var(--amber); border: 1px solid var(--med-bd); }
        .risk.LOW { background: var(--low-bg); color: var(--muted); border: 1px solid var(--low-bd); }
        .risk.NONE { background: rgba(148,163,184,0.08); color: var(--faint); border: 1px solid var(--border); }
        .pii { display: inline-block; min-width: 20px; padding: 1px 6px; border-radius: 10px; font-size: 0.68rem; font-weight: 700; }
        .pii.hot { background: var(--high-bg); color: var(--red); }
        .reach { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; background: rgba(96,165,250,0.14); color: var(--blue); border: 1px solid rgba(96,165,250,0.3); }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .footer { max-width: 1400px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Cross-Database Compliance</span>
        <h1>Access across {{ report.databases|length }} databases</h1>
        <p>Generated on {{ generated_at }} &middot; every principal and their read/write/admin access to regulated data, side by side</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-violet"><div class="number">{{ summary.databases }}</div><div class="label">Databases</div></div>
            <div class="stat-card c-blue"><div class="number">{{ summary.principals }}</div><div class="label">Principals</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.cross_db_principals }}</div><div class="label">Cross-DB principals</div></div>
            <div class="stat-card c-green"><div class="number">{{ summary.principals_with_pii }}</div><div class="label">With PII access</div></div>
            <div class="stat-card c-red"><div class="number">{{ summary.high_risk_principals }}</div><div class="label">HIGH-risk principals</div></div>
        </div>

        {% if report.errors %}
        <div class="warn">
            {% for db, msg in report.errors %}<div>&bull; <b>{{ db }}</b> — {{ msg }}</div>{% endfor %}
        </div>
        {% endif %}

        <h2 class="section">Access matrix <span class="n">principal &times; database</span></h2>
        <div class="panel">
            <table>
                <thead>
                    <tr>
                        <th>Principal</th>
                        <th>Reach</th>
                        <th>Worst risk</th>
                        <th>PII objs</th>
                        {% for db in report.databases %}<th class="dbcol">{{ db }}</th>{% endfor %}
                    </tr>
                </thead>
                <tbody>
                    {% for p in report.principals %}
                    <tr>
                        <td class="princ">{{ p.principal }}{% if p.is_role %}<span class="ptype">ROLE</span>{% endif %}</td>
                        <td><span class="reach">{{ p.database_count }} / {{ report.databases|length }}</span></td>
                        <td><span class="risk {{ p.max_risk }}">{{ p.max_risk }}</span></td>
                        <td>{% if p.total_pii_objects %}<span class="pii hot">{{ p.total_pii_objects }}</span>{% else %}<span class="none">0</span>{% endif %}</td>
                        {% for db in report.databases %}
                        {% set pa = p.per_db.get(db) %}
                        <td class="dbcell">
                            {% if pa and pa.object_count %}
                                {% for lv in pa.levels %}<span class="level {{ lv }}">{{ lv }}</span>{% endfor %}
                                {% if pa.pii_object_count %}<div><span class="risk {{ pa.max_risk }}" style="margin-top:3px;">{{ pa.pii_object_count }} PII</span></div>{% endif %}
                            {% else %}
                                <span class="none">&mdash;</span>
                            {% endif %}
                        </td>
                        {% endfor %}
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.principals %}<div class="empty">No principals with object grants across the configured databases.</div>{% endif %}
        </div>
    </div>
    <div class="footer">
        <strong>Board-level scope.</strong> Each database's access audit reads object-level GRANTs
        (<code>sys.database_permissions</code> on SQL Server; <code>information_schema.table_privileges</code> on PostgreSQL/MySQL),
        buckets them into read/write/admin, and cross-references PII-bearing tables from the name/type scanner. A principal name is
        assumed to represent the same identity across databases. Role membership and server-level rights are not resolved here.
        No table row data was read.
    </div>
</body>
</html>
"""


def build_multi_comply_json(report) -> dict:
    def pa_row(pa):
        if pa is None:
            return None
        return {"levels": pa.levels, "object_count": pa.object_count,
                "pii_object_count": pa.pii_object_count, "max_risk": pa.max_risk,
                "regulations": pa.regulations, "is_role": pa.is_role}
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "compliance-multi",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "databases": list(report.databases),
        "summary": summarize_multi(report),
        "principals": [{
            "principal": p.principal,
            "is_role": p.is_role,
            "database_count": p.database_count,
            "total_pii_objects": p.total_pii_objects,
            "max_risk": p.max_risk,
            "levels": p.levels,
            "per_database": {db: pa_row(p.per_db.get(db)) for db in report.databases},
        } for p in report.principals],
        "errors": [{"database": db, "message": m} for db, m in report.errors],
    }


def render_multi_comply_html(report, output_path):
    template = Environment(autoescape=True).from_string(MULTI_TEMPLATE)
    html = template.render(
        report=report,
        summary=summarize_multi(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Cross-database compliance report written to {output_path}")
