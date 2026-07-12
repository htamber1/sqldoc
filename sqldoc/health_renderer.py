"""HTML + JSON rendering for the `sqldoc health` report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.health import summarize

HEALTH_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ database }} — Database Health</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(96,165,250,0.12), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--blue), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1200px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 22px; text-align: center; }
        .stat-card .number { font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.78rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.07em; }
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
        .mono { font-family: 'Consolas', monospace; }
        .loc { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .num { text-align: right; font-family: 'Consolas', monospace; white-space: nowrap; }
        .sql { font-family: 'Consolas', monospace; font-size: 0.78rem; color: #cbd5e1; white-space: pre-wrap; word-break: break-word; max-width: 560px; }
        .pill { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; border: 1px solid transparent; }
        .pill.reb { background: rgba(220,38,38,0.15); color: var(--red); border-color: rgba(220,38,38,0.4); }
        .pill.reo { background: rgba(245,158,11,0.15); color: var(--amber); border-color: rgba(245,158,11,0.4); }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .bar { display: inline-block; height: 7px; border-radius: 4px; background: linear-gradient(90deg, var(--amber), var(--red)); vertical-align: middle; margin-right: 8px; }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 20px; color: var(--amber); font-size: 0.83rem; }
        .footer { max-width: 1200px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Database Health</span>
        <h1>{{ database }}</h1>
        <p>Generated on {{ generated_at }} &middot; from server/DB statistics (no table row data read)</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-red"><div class="number">{{ summary.slow_queries }}</div><div class="label">Slow queries</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.dead_tables }}</div><div class="label">Dead tables</div></div>
            <div class="stat-card c-blue"><div class="number">{{ summary.missing_indexes }}</div><div class="label">Missing indexes</div></div>
            <div class="stat-card c-violet"><div class="number">{{ summary.fragmented_indexes }}</div><div class="label">Fragmented indexes</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.unused_procedures }}</div><div class="label">Unused procedures</div></div>
            <div class="stat-card c-blue"><div class="number">{{ summary.duplicate_tables }}</div><div class="label">Duplicate tables</div></div>
            <div class="stat-card c-red"><div class="number">{{ summary.redundant_indexes }}</div><div class="label">Redundant indexes</div></div>
        </div>

        {% if report.errors %}
        <div class="warn">
            Some checks were skipped (usually a missing <b>VIEW SERVER STATE</b> permission):
            {% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }}</div>{% endfor %}
        </div>
        {% endif %}

        <h2 class="section">Slow queries <span class="n">by average elapsed time</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Query</th><th>Avg ms</th><th>Execs</th><th>Avg reads</th><th>Last run</th></tr></thead>
                <tbody>
                    {% for q in report.slow_queries %}
                    <tr>
                        <td class="sql">{{ q.query_text }}</td>
                        <td class="num">{{ '%.1f'|format(q.avg_elapsed_ms) }}</td>
                        <td class="num">{{ '{:,}'.format(q.execution_count) }}</td>
                        <td class="num">{{ '{:,}'.format(q.avg_logical_reads) }}</td>
                        <td class="mono">{{ q.last_execution }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.slow_queries %}<div class="empty">No cached query statistics available.</div>{% endif %}
        </div>

        <h2 class="section">Dead tables <span class="n">rows present, no reads since stats reset</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Table</th><th>Rows</th><th>Writes</th><th>Reads</th><th>Last read</th></tr></thead>
                <tbody>
                    {% for d in report.dead_tables %}
                    <tr>
                        <td class="loc">{{ d.schema }}.{{ d.table }}</td>
                        <td class="num">{{ '{:,}'.format(d.row_count) }}</td>
                        <td class="num">{{ '{:,}'.format(d.user_updates) }}</td>
                        <td class="num">{{ d.reads }}</td>
                        <td class="mono">{{ d.last_read or '—' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.dead_tables %}<div class="empty">No dead tables detected.</div>{% endif %}
        </div>

        <h2 class="section">Missing indexes <span class="n">ranked by estimated benefit</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Table</th><th>Impact %</th><th>Seeks</th><th>Suggested index</th></tr></thead>
                <tbody>
                    {% for m in report.missing_indexes %}
                    <tr>
                        <td class="loc">{{ m.schema }}.{{ m.table }}</td>
                        <td class="num"><span class="bar" style="width: {{ (m.avg_user_impact / 100 * 60)|round|int }}px;"></span>{{ '%.0f'|format(m.avg_user_impact) }}</td>
                        <td class="num">{{ '{:,}'.format(m.user_seeks) }}</td>
                        <td class="sql">{{ m.create_statement() }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.missing_indexes %}<div class="empty">No missing-index recommendations.</div>{% endif %}
        </div>

        <h2 class="section">Index fragmentation <span class="n">large indexes past threshold</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Index</th><th>Table</th><th>Fragmentation</th><th>Pages</th><th>Action</th></tr></thead>
                <tbody>
                    {% for f in report.fragmented_indexes %}
                    <tr>
                        <td class="mono">{{ f.index_name }}</td>
                        <td class="loc">{{ f.schema }}.{{ f.table }}</td>
                        <td class="num">{{ '%.1f'|format(f.avg_fragmentation_percent) }}%</td>
                        <td class="num">{{ '{:,}'.format(f.page_count) }}</td>
                        <td><span class="pill {{ 'reb' if f.recommendation == 'REBUILD' else 'reo' }}">{{ f.recommendation }}</span></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.fragmented_indexes %}<div class="empty">No fragmented indexes past the threshold.</div>{% endif %}
        </div>

        <h2 class="section">Unused procedures <span class="n">no execution recorded since stats reset</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Procedure</th><th>Executions</th><th>Created</th><th>Last modified</th></tr></thead>
                <tbody>
                    {% for p in report.unused_procedures %}
                    <tr>
                        <td class="loc">{{ p.schema }}.{{ p.name }}</td>
                        <td class="num">{{ '{:,}'.format(p.execution_count) }}</td>
                        <td class="mono">{{ p.created or '—' }}</td>
                        <td class="mono">{{ p.modified or '—' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.unused_procedures %}<div class="empty">No unused procedures detected (or execution stats unavailable).</div>{% endif %}
        </div>

        <h2 class="section">Duplicate tables <span class="n">similar names &amp; overlapping columns</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Table A</th><th>Table B</th><th>Name match</th><th>Column overlap</th><th>Shared columns</th><th>Confidence</th></tr></thead>
                <tbody>
                    {% for d in report.duplicate_tables %}
                    <tr>
                        <td class="loc">{{ d.a }}</td>
                        <td class="loc">{{ d.b }}</td>
                        <td class="num">{{ (d.name_similarity * 100)|round|int }}%</td>
                        <td class="num">{{ (d.column_overlap * 100)|round|int }}%</td>
                        <td class="mono" style="max-width: 320px; white-space: normal;">{{ d.shared_columns|join(', ') }}</td>
                        <td class="num"><span class="bar" style="width: {{ (d.confidence * 60)|round|int }}px;"></span>{{ (d.confidence * 100)|round|int }}%</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.duplicate_tables %}<div class="empty">No potential duplicate tables detected.</div>{% endif %}
        </div>

        <h2 class="section">Redundant indexes <span class="n">duplicate or prefix-covered on the same table</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Index</th><th>Table</th><th>Key columns</th><th>Covered by</th><th>Reason</th></tr></thead>
                <tbody>
                    {% for r in report.redundant_indexes %}
                    <tr>
                        <td class="mono">{{ r.index_name }}</td>
                        <td class="loc">{{ r.schema }}.{{ r.table }}</td>
                        <td class="mono">{{ r.key_columns|join(', ') }}</td>
                        <td class="mono">{{ r.covered_by }}</td>
                        <td><span class="pill {{ 'reb' if r.reason == 'duplicate' else 'reo' }}">{{ r.reason }}</span></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.redundant_indexes %}<div class="empty">No redundant indexes detected.</div>{% endif %}
        </div>
    </div>
    <div class="footer">
        <strong>About these metrics.</strong> All figures come from SQL Server DMVs and reflect activity <em>since the statistics last reset</em>
        (a service restart or <code>DBCC</code> clears them), so a freshly restarted server can look artificially quiet. Missing-index
        suggestions are optimizer hints, not guarantees — validate before creating indexes. <strong>Unused procedures</strong> means no
        execution since the stats reset (verify before dropping). <strong>Duplicate tables</strong> and <strong>redundant indexes</strong>
        are inferred from schema metadata (names, columns, index keys) — dialect-neutral and reading no row data.
    </div>
</body>
</html>
"""


def build_health_json(database: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "health",
        "database": database,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(report),
        "slow_queries": [asdict(q) for q in report.slow_queries],
        "dead_tables": [asdict(d) for d in report.dead_tables],
        "missing_indexes": [{**asdict(m), "create_statement": m.create_statement()}
                            for m in report.missing_indexes],
        "fragmented_indexes": [{**asdict(f), "recommendation": f.recommendation}
                               for f in report.fragmented_indexes],
        "unused_procedures": [asdict(p) for p in report.unused_procedures],
        "duplicate_tables": [asdict(d) for d in report.duplicate_tables],
        "redundant_indexes": [asdict(r) for r in report.redundant_indexes],
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_health_html(database, report, output_path):
    report.database = database
    template = Environment(autoescape=True).from_string(HEALTH_TEMPLATE)
    html = template.render(
        database=database,
        report=report,
        summary=summarize(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Health report written to {output_path}")
