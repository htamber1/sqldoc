"""HTML + JSON rendering for the `sqldoc dbt` unified documentation."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.dbt import summarize

DBT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ project }} — dbt + Database Docs</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --bg: #0a0a0f; --card: #1e2530; --card-head: #171d26;
            --text: #e5e7eb; --text-strong: #f8fafc; --muted: #94a3b8; --faint: #64748b;
            --border: #2a3340; --border-strong: #3a4658;
            --red: #f87171; --amber: #fbbf24; --green: #34d399; --blue: #60a5fa; --violet: #c084fc; --orange: #fb923c;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; }
        ::-webkit-scrollbar { width: 11px; height: 11px; }
        ::-webkit-scrollbar-track { background: #0a0e18; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 6px; border: 2px solid #0a0e18; }
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(251,146,60,0.12), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--orange), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1200px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 22px; text-align: center; }
        .stat-card .number { font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.74rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.06em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); }
        .c-blue .number { color: var(--blue); } .c-green .number { color: var(--green); }
        .c-violet .number { color: var(--violet); } .c-orange .number { color: var(--orange); }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 30px 0 12px; display: flex; align-items: center; gap: 10px; }
        h2.section .n { font-size: 0.8rem; color: var(--muted); font-weight: 600; }
        .model { background: var(--card); border: 1px solid var(--border); border-radius: 14px; margin-bottom: 18px; overflow: hidden; }
        .model-head { display: flex; align-items: center; gap: 12px; padding: 16px 20px; background: var(--card-head); border-bottom: 1px solid var(--border-strong); flex-wrap: wrap; }
        .model-head .name { font-size: 1.05rem; font-weight: 800; color: var(--text-strong); font-family: 'Consolas', monospace; }
        .model-head .desc { color: var(--muted); font-size: 0.85rem; flex: 1 1 100%; }
        .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.68rem; font-weight: 700; border: 1px solid transparent; }
        .badge.mat { background: rgba(192,132,252,0.14); color: var(--violet); border-color: rgba(192,132,252,0.3); }
        .badge.indb { background: rgba(52,211,153,0.14); color: var(--green); border-color: rgba(52,211,153,0.35); }
        .badge.nodb { background: rgba(245,158,11,0.14); color: var(--amber); border-color: rgba(245,158,11,0.4); }
        .badge.tbl { background: rgba(96,165,250,0.14); color: var(--blue); border-color: rgba(96,165,250,0.3); font-family: 'Consolas', monospace; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 10px 16px; text-align: left; font-size: 0.7rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 9px 16px; font-size: 0.84rem; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        .col { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .type { font-family: 'Consolas', monospace; color: var(--blue); font-size: 0.8rem; }
        .st { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.66rem; font-weight: 700; }
        .st.matched { background: rgba(52,211,153,0.14); color: var(--green); }
        .st.dbt-only { background: rgba(248,113,113,0.14); color: var(--red); }
        .st.db-only { background: rgba(245,158,11,0.14); color: var(--amber); }
        .test { display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 0.66rem; background: rgba(255,255,255,0.05); border: 1px solid var(--border); margin: 1px 3px 1px 0; font-family: 'Consolas', monospace; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        .muted { color: var(--muted); }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .footer { max-width: 1200px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; dbt integration</span>
        <h1>{{ project }}</h1>
        <p>Generated on {{ generated_at }} &middot; dbt model metadata unified with the live database schema</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-orange"><div class="number">{{ summary.models }}</div><div class="label">dbt models</div></div>
            <div class="stat-card c-green"><div class="number">{{ summary.matched_in_db }}</div><div class="label">Matched in DB</div></div>
            <div class="stat-card c-blue"><div class="number">{{ summary.doc_coverage_pct }}%</div><div class="label">Column doc coverage</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.undocumented_db_columns }}</div><div class="label">Undocumented DB cols</div></div>
            <div class="stat-card c-red"><div class="number">{{ summary.drifted_columns }}</div><div class="label">Drifted cols</div></div>
        </div>

        {% if doc.warnings %}
        <div class="warn">{% for w in doc.warnings %}<div>&bull; {{ w }}</div>{% endfor %}</div>
        {% endif %}

        <h2 class="section">Models <span class="n">dbt description + actual database columns</span></h2>
        {% for m in doc.models %}
        <div class="model">
            <div class="model-head">
                <span class="name">{{ m.name }}</span>
                {% if m.materialized %}<span class="badge mat">{{ m.materialized }}</span>{% endif %}
                {% if m.in_db %}<span class="badge indb">in database</span><span class="badge tbl">{{ m.matched_table }}</span>{% if m.row_count is not none %}<span class="muted" style="font-size:0.78rem;">{{ '{:,}'.format(m.row_count) }} rows</span>{% endif %}{% else %}<span class="badge nodb">not found in DB</span>{% endif %}
                {% if m.dbt_description %}<div class="desc">{{ m.dbt_description }}</div>{% endif %}
            </div>
            <div style="overflow-x:auto;">
            <table>
                <thead><tr><th>Column</th><th>DB type</th><th>Status</th><th>dbt description</th><th>Tests</th></tr></thead>
                <tbody>
                    {% for c in m.columns %}
                    <tr>
                        <td class="col">{{ c.name }}</td>
                        <td class="type">{{ c.db_type or '—' }}</td>
                        <td><span class="st {{ c.status }}">{{ c.status }}</span></td>
                        <td>{{ c.dbt_description or (c.db_description and ('(db) ' ~ c.db_description)) or '' }}</td>
                        <td>{% for t in c.tests %}<span class="test">{{ t }}</span>{% endfor %}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            </div>
        </div>
        {% endfor %}
        {% if not doc.models %}<div class="empty">No dbt models found.</div>{% endif %}

        {% if doc.unmatched_db_tables %}
        <h2 class="section">Database tables without a dbt model <span class="n">not modeled in dbt</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Table</th></tr></thead>
                <tbody>{% for t in doc.unmatched_db_tables %}<tr><td class="col">{{ t }}</td></tr>{% endfor %}</tbody>
            </table>
        </div>
        {% endif %}
    </div>
    <div class="footer">
        <strong>Method.</strong> dbt metadata is read from <code>dbt_project.yml</code> and the <code>schema.yml</code> files under
        the project's model paths. Each model is matched to a database table by name (case-insensitive). <em>db-only</em> columns exist
        in the database but have no dbt documentation; <em>dbt-only</em> columns are documented in dbt but were not found in the database
        (possible drift). No table row data was read.
    </div>
</body>
</html>
"""


def build_dbt_json(project_name: str, doc) -> dict:
    def col(c):
        return {"name": c.name, "status": c.status, "in_dbt": c.in_dbt, "in_db": c.in_db,
                "db_type": c.db_type, "dbt_description": c.dbt_description,
                "db_description": c.db_description, "tests": c.tests}
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "dbt",
        "project": project_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(doc),
        "models": [{
            "name": m.name,
            "dbt_description": m.dbt_description,
            "materialized": m.materialized,
            "in_db": m.in_db,
            "matched_table": m.matched_table,
            "row_count": m.row_count,
            "columns": [col(c) for c in m.columns],
        } for m in doc.models],
        "unmatched_db_tables": list(doc.unmatched_db_tables),
        "warnings": list(doc.warnings),
    }


def render_dbt_html(project_name, doc, output_path):
    template = Environment(autoescape=True).from_string(DBT_TEMPLATE)
    html = template.render(
        project=project_name,
        doc=doc,
        summary=summarize(doc),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"dbt documentation written to {output_path}")
