"""HTML + JSON rendering for the `sqldoc insights` report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.insights import summarize

INSIGHTS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ database }} — AI Insights</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(96,165,250,0.13), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--blue), var(--violet) 60%, transparent 90%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1200px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 22px; text-align: center; }
        .stat-card .number { font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.76rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.07em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); }
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
        .muted { color: var(--muted); font-size: 0.82rem; }
        .sev { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; border: 1px solid transparent; }
        .sev.HIGH { background: rgba(220,38,38,0.15); color: var(--red); border-color: rgba(220,38,38,0.4); }
        .sev.MEDIUM { background: rgba(245,158,11,0.15); color: var(--amber); border-color: rgba(245,158,11,0.4); }
        .sev.LOW { background: rgba(148,163,184,0.12); color: var(--muted); border-color: rgba(148,163,184,0.3); }
        .kind { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.68rem; font-weight: 700; background: rgba(96,165,250,0.14); color: var(--blue); border: 1px solid rgba(96,165,250,0.3); }
        pre.sql { margin: 0; padding: 14px 16px; background: #0c1119; color: #cbd5e1; font-family: 'Consolas', monospace; font-size: 0.8rem; line-height: 1.5; white-space: pre-wrap; word-break: break-word; border-radius: 8px; }
        .q { font-weight: 600; color: var(--text-strong); margin-bottom: 8px; }
        .qcard { padding: 16px; border-bottom: 1px solid var(--border); }
        .qcard:last-child { border-bottom: none; }
        .bar { display: inline-block; height: 7px; border-radius: 4px; background: linear-gradient(90deg, var(--amber), var(--green)); vertical-align: middle; margin-right: 8px; }
        .glossary-search { width: 100%; padding: 11px 14px; margin-bottom: 12px; background: var(--card); border: 1px solid var(--border-strong); border-radius: 10px; color: var(--text); font-size: 0.9rem; }
        .term { font-weight: 700; color: var(--text-strong); }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .footer { max-width: 1200px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; AI Insights</span>
        <h1>{{ database }}</h1>
        <p>Generated on {{ generated_at }} &middot; anomalies, relationships{{ ', glossary' if report.glossary else '' }}{{ ', NL→SQL' if report.queries else '' }}</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-red"><div class="number">{{ summary.anomalies }}</div><div class="label">Anomalies</div></div>
            <div class="stat-card c-blue"><div class="number">{{ summary.relationships }}</div><div class="label">Suggested FKs</div></div>
            <div class="stat-card c-violet"><div class="number">{{ summary.glossary_terms }}</div><div class="label">Glossary terms</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.queries }}</div><div class="label">Queries built</div></div>
        </div>

        {% if report.queries %}
        <h2 class="section">Natural-language queries <span class="n">schema-grounded T-SQL</span></h2>
        <div class="panel">
            {% for q in report.queries %}
            <div class="qcard">
                <div class="q">{{ q.question }}</div>
                <pre class="sql">{{ q.sql }}</pre>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        <h2 class="section">Schema anomalies <span class="n">architectural smells</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Severity</th><th>Kind</th><th>Object</th><th>Detail</th><th>Recommendation</th></tr></thead>
                <tbody>
                    {% for a in report.anomalies %}
                    <tr>
                        <td><span class="sev {{ a.severity }}">{{ a.severity }}</span></td>
                        <td><span class="kind">{{ a.kind }}</span></td>
                        <td class="loc">{{ a.object }}</td>
                        <td class="muted">{{ a.detail }}</td>
                        <td class="muted">{{ a.recommendation }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.anomalies %}<div class="empty">No anomalies detected.</div>{% endif %}
        </div>

        <h2 class="section">Inferred relationships <span class="n">likely FKs with no constraint</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>From</th><th>To</th><th>Confidence</th><th>Suggested constraint</th></tr></thead>
                <tbody>
                    {% for r in report.relationships %}
                    <tr>
                        <td class="loc">{{ r.from_table }}.{{ r.from_column }}</td>
                        <td class="loc">{{ r.to_table }}.{{ r.to_column }}</td>
                        <td><span class="bar" style="width: {{ (r.confidence * 60)|round|int }}px;"></span>{{ '%.0f'|format(r.confidence * 100) }}%</td>
                        <td><pre class="sql">{{ r.ddl }}</pre></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.relationships %}<div class="empty">No missing relationships inferred.</div>{% endif %}
        </div>

        {% if report.glossary %}
        <h2 class="section">Business glossary <span class="n">AI-inferred terms</span></h2>
        <input type="text" class="glossary-search" id="gsearch" placeholder="Search terms and definitions…">
        <div class="panel">
            <table>
                <thead><tr><th>Term</th><th>Schema</th><th>Definition</th><th>Source</th></tr></thead>
                <tbody id="glossary">
                    {% for g in report.glossary %}
                    <tr data-text="{{ (g.term ~ ' ' ~ g.definition ~ ' ' ~ g.source)|lower }}">
                        <td class="term">{{ g.term }}</td>
                        <td class="muted">{{ g.category }}</td>
                        <td>{{ g.definition }}</td>
                        <td class="loc">{{ g.source }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            <div class="empty" id="no-match" style="display:none;">No matching terms.</div>
        </div>
        {% endif %}
    </div>
    <div class="footer">
        <strong>Heuristic + AI.</strong> Anomalies and relationship inference are heuristic (name/type patterns) — review before acting.
        Generated SQL and glossary definitions come from a language model grounded in schema metadata only (no row data was read); verify
        queries before running them and definitions before publishing.
    </div>
    <script>
        (function () {
            var box = document.getElementById('gsearch');
            if (!box) { return; }
            var rows = Array.prototype.slice.call(document.querySelectorAll('#glossary tr'));
            var noMatch = document.getElementById('no-match');
            box.addEventListener('input', function () {
                var q = box.value.toLowerCase().trim();
                var shown = 0;
                rows.forEach(function (r) {
                    var ok = !q || r.getAttribute('data-text').indexOf(q) >= 0;
                    r.style.display = ok ? '' : 'none';
                    if (ok) { shown++; }
                });
                noMatch.style.display = shown ? 'none' : 'block';
            });
        })();
    </script>
</body>
</html>
"""


def build_insights_json(database: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "insights",
        "database": database,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(report),
        "queries": [asdict(q) for q in report.queries],
        "anomalies": [asdict(a) for a in report.anomalies],
        "relationships": [asdict(r) for r in report.relationships],
        "glossary": [asdict(g) for g in report.glossary],
        "errors": [{"context": c, "message": m} for c, m in report.errors],
    }


def render_insights_html(database, report, output_path):
    report.database = database
    template = Environment(autoescape=True).from_string(INSIGHTS_TEMPLATE)
    html = template.render(
        database=database,
        report=report,
        summary=summarize(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Insights report written to {output_path}")
