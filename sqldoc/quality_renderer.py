"""HTML + JSON rendering for the `sqldoc quality` report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.quality import summarize

QUALITY_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ database }} — Data Quality</title>
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
        .container { max-width: 1240px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 22px; text-align: center; }
        .stat-card .number { font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.78rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.07em; }
        .c-green .number { color: var(--green); } .c-amber .number { color: var(--amber); }
        .c-red .number { color: var(--red); } .c-blue .number { color: var(--blue); }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 30px 0 12px; display: flex; align-items: center; gap: 10px; }
        h2.section .n { font-size: 0.8rem; color: var(--muted); font-weight: 600; }
        .toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
        .filter-btn { padding: 6px 14px; font-size: 0.82rem; font-weight: 600; color: var(--muted); background: var(--card); border: 1px solid var(--border); border-radius: 8px; cursor: pointer; }
        .filter-btn.active { background: rgba(96,165,250,0.15); color: var(--blue); border-color: var(--blue); }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 14px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 10px 14px; font-size: 0.84rem; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255,255,255,0.025); }
        .loc { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .col { color: var(--blue); font-weight: 600; }
        .dtype { color: var(--muted); font-family: 'Consolas', monospace; font-size: 0.78rem; }
        .num { text-align: right; font-family: 'Consolas', monospace; white-space: nowrap; }
        .meter { position: relative; width: 90px; height: 8px; border-radius: 5px; background: #10151d; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 8px; }
        .meter > span { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 5px; }
        .flag { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.68rem; font-weight: 700; margin: 1px 3px 1px 0; }
        .flag.high-null { background: rgba(220,38,38,0.15); color: var(--red); border: 1px solid rgba(220,38,38,0.4); }
        .flag.constant { background: rgba(245,158,11,0.15); color: var(--amber); border: 1px solid rgba(245,158,11,0.4); }
        .flag.blanks { background: rgba(168,85,247,0.15); color: var(--violet); border: 1px solid rgba(168,85,247,0.35); }
        .vals { color: #cbd5e1; font-size: 0.78rem; }
        .vals code { background: rgba(255,255,255,0.05); padding: 1px 6px; border-radius: 4px; margin-right: 5px; }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .footer { max-width: 1240px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Data Quality</span>
        <h1>{{ database }}</h1>
        <p>Generated on {{ generated_at }} &middot; aggregate profiling (COUNT / DISTINCT / MIN / MAX / GROUP BY)</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-blue"><div class="number">{{ summary.columns_profiled }}</div><div class="label">Columns profiled</div></div>
            <div class="stat-card c-red"><div class="number">{{ summary.high_null_columns }}</div><div class="label">High-null columns</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.constant_columns }}</div><div class="label">Constant columns</div></div>
            <div class="stat-card c-green"><div class="number">{{ summary.tables_with_duplicates }}</div><div class="label">Tables with dupes</div></div>
        </div>

        <h2 class="section">Duplicate records <span class="n">full-row duplicates by GROUP BY</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Table</th><th>Duplicate groups</th><th>Redundant rows</th><th>Columns considered</th></tr></thead>
                <tbody>
                    {% for d in report.duplicates %}
                    <tr>
                        <td class="loc">{{ d.schema }}.{{ d.table }}</td>
                        <td class="num">{{ '{:,}'.format(d.duplicate_groups) }}</td>
                        <td class="num">{{ '{:,}'.format(d.duplicate_rows) }}</td>
                        <td class="dtype">{{ d.columns_considered|join(', ') }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.duplicates %}<div class="empty">No fully-duplicated rows detected.</div>{% endif %}
        </div>

        <h2 class="section">Column profile <span class="n">null rate, cardinality, distribution</span></h2>
        <div class="toolbar">
            <button type="button" class="filter-btn active" data-filter="all">All ({{ report.columns|length }})</button>
            <button type="button" class="filter-btn" data-filter="high-null">High null</button>
            <button type="button" class="filter-btn" data-filter="constant">Constant</button>
            <button type="button" class="filter-btn" data-filter="blanks">Blanks</button>
        </div>
        <div class="panel">
            <table>
                <thead><tr><th>Column</th><th>Type</th><th>Rows</th><th>Null rate</th><th>Distinct</th><th>Min / Max</th><th>Top values</th><th>Flags</th></tr></thead>
                <tbody id="cols">
                    {% for c in report.columns %}
                    <tr data-flags="{{ c.flags|join(' ') }}">
                        <td class="loc">{{ c.schema }}.{{ c.table }}.<span class="col">{{ c.column }}</span></td>
                        <td class="dtype">{{ c.data_type }}</td>
                        <td class="num">{{ '{:,}'.format(c.total_rows) }}</td>
                        <td class="num">
                            <span class="meter"><span style="width: {{ (c.null_rate * 100)|round|int }}%; background: {{ '#f87171' if c.null_rate >= 0.5 else '#34d399' }};"></span></span>
                            {{ '%.1f'|format(c.null_rate * 100) }}%
                        </td>
                        <td class="num">{{ '{:,}'.format(c.distinct_count) if c.distinct_count >= 0 else '—' }}</td>
                        <td class="dtype">{% if c.min_value or c.max_value %}{{ c.min_value }} … {{ c.max_value }}{% else %}—{% endif %}</td>
                        <td class="vals">{% for v in c.top_values %}<code>{{ v.value if v.value != '' else '∅' }}</code>{% endfor %}</td>
                        <td>{% for fl in c.flags %}<span class="flag {{ fl }}">{{ fl }}</span>{% endfor %}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.columns %}<div class="empty">No columns profiled.</div>{% endif %}
            <div class="empty" id="no-match" style="display:none;">No columns with that flag.</div>
        </div>

        {% if report.errors %}
        <h2 class="section">Skipped <span class="n">columns/tables that could not be profiled</span></h2>
        <div class="panel"><table><tbody>
            {% for ctx, msg in report.errors %}<tr><td class="loc">{{ ctx }}</td><td class="dtype">{{ msg }}</td></tr>{% endfor %}
        </tbody></table></div>
        {% endif %}
    </div>
    <div class="footer">
        <strong>What was read.</strong> This report is built from aggregate queries over your data — counts, distinct counts, min/max, and
        GROUP BY for duplicates. Each column's most-frequent values are shown for context and are truncated; <strong>nothing is sent to any
        AI or off this machine.</strong> Null/duplicate figures reflect the data at scan time.
    </div>
    <script>
        (function () {
            var rows = Array.prototype.slice.call(document.querySelectorAll('#cols tr'));
            var btns = Array.prototype.slice.call(document.querySelectorAll('.filter-btn'));
            var noMatch = document.getElementById('no-match');
            btns.forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var f = btn.getAttribute('data-filter');
                    btns.forEach(function (b) { b.classList.toggle('active', b === btn); });
                    var shown = 0;
                    rows.forEach(function (r) {
                        var ok = (f === 'all' || (' ' + r.getAttribute('data-flags') + ' ').indexOf(' ' + f + ' ') >= 0);
                        r.style.display = ok ? '' : 'none';
                        if (ok) { shown++; }
                    });
                    noMatch.style.display = shown ? 'none' : 'block';
                });
            });
        })();
    </script>
</body>
</html>
"""


def build_quality_json(database: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "quality",
        "database": database,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(report),
        "columns": [{**asdict(c), "distinct_rate": round(c.distinct_rate, 4),
                     "is_constant": c.is_constant, "flags": c.flags}
                    for c in report.columns],
        "duplicates": [asdict(d) for d in report.duplicates],
        "errors": [{"context": ctx, "message": msg} for ctx, msg in report.errors],
    }


def render_quality_html(database, report, output_path):
    report.database = database
    template = Environment(autoescape=True).from_string(QUALITY_TEMPLATE)
    html = template.render(
        database=database,
        report=report,
        summary=summarize(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Data-quality report written to {output_path}")
