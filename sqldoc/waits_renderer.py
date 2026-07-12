"""HTML + JSON rendering for the `sqldoc waits` report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.waits import summarize, CATEGORIES

_CAT_COLORS = {"IO": "#60a5fa", "Lock": "#f87171", "Memory": "#c084fc",
               "CPU": "#fbbf24", "Network": "#34d399", "Other": "#64748b"}

WAITS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ target }} — Wait Statistics</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(251,191,36,0.12), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--amber), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1100px; margin: 0 auto; padding: 36px 20px 20px; }
        .cats { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 22px 26px; margin-bottom: 24px; }
        .cats h3 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); margin-bottom: 16px; }
        .stackbar { display: flex; height: 26px; border-radius: 8px; overflow: hidden; margin-bottom: 14px; }
        .stackbar .seg { height: 100%; }
        .legend { display: flex; gap: 18px; flex-wrap: wrap; }
        .legend .item { display: flex; align-items: center; gap: 7px; font-size: 0.82rem; }
        .legend .dot { width: 11px; height: 11px; border-radius: 3px; }
        .ai { background: linear-gradient(180deg, #202a38, var(--card)); border: 1px solid rgba(96,165,250,0.3); border-radius: 14px; padding: 20px 24px; margin-bottom: 24px; }
        .ai h3 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.07em; color: var(--blue); margin-bottom: 10px; }
        .ai .body { color: #dbe4ee; font-size: 0.9rem; line-height: 1.6; white-space: pre-wrap; }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 26px 0 12px; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 16px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 10px 16px; font-size: 0.85rem; border-bottom: 1px solid var(--border); }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255,255,255,0.025); }
        .mono { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .num { text-align: right; font-family: 'Consolas', monospace; white-space: nowrap; }
        .catlabel { display: inline-block; padding: 2px 9px; border-radius: 5px; font-size: 0.68rem; font-weight: 700; }
        .pctbar { display: inline-block; height: 8px; border-radius: 4px; vertical-align: middle; margin-right: 8px; }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .footer { max-width: 1100px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Wait Statistics</span>
        <h1>{{ target }}</h1>
        <p>Generated on {{ generated_at }} &middot; {{ report.dialect }} {{ 'point-in-time snapshot' if report.snapshot else 'cumulative wait stats' }} &middot; top category: {{ summary.top_category }}</p>
    </div>
    <div class="container">
        {% if report.errors %}
        <div class="warn">{% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }}</div>{% endfor %}</div>
        {% endif %}

        {% if report.waits %}
        <div class="cats">
            <h3>Wait categories ({{ 'sessions waiting' if report.snapshot else '% of total wait time' }})</h3>
            <div class="stackbar">
                {% for cat, pct in summary.category_percent.items() %}<div class="seg" style="width: {{ pct }}%; background: {{ cat_colors[cat] }};" title="{{ cat }}: {{ pct }}%"></div>{% endfor %}
            </div>
            <div class="legend">
                {% for cat, pct in summary.category_percent.items() %}<div class="item"><span class="dot" style="background: {{ cat_colors[cat] }};"></span>{{ cat }} — {{ pct }}%</div>{% endfor %}
            </div>
        </div>

        {% if report.ai_explanation %}
        <div class="ai">
            <h3>AI analysis</h3>
            <div class="body">{{ report.ai_explanation }}</div>
        </div>
        {% endif %}

        <h2 class="section">Top waits</h2>
        <div class="panel">
            <table>
                <thead><tr><th>Wait type</th><th>Category</th><th>{{ 'Sessions' if report.snapshot else 'Wait (ms)' }}</th><th>Tasks</th><th>Share</th></tr></thead>
                <tbody>
                    {% for w in report.waits %}
                    <tr>
                        <td class="mono">{{ w.wait_type }}</td>
                        <td><span class="catlabel" style="background: {{ cat_colors[w.category] }}22; color: {{ cat_colors[w.category] }};">{{ w.category }}</span></td>
                        <td class="num">{% if report.snapshot %}{{ w.waiting_tasks }}{% else %}{{ '{:,.0f}'.format(w.wait_time_ms) }}{% endif %}</td>
                        <td class="num">{{ '{:,}'.format(w.waiting_tasks) }}</td>
                        <td class="num"><span class="pctbar" style="width: {{ (w.percent * 0.8)|round|int }}px; background: {{ cat_colors[w.category] }};"></span>{{ w.percent }}%</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <div class="empty">No significant waits recorded.</div>
        {% endif %}
    </div>
    <div class="footer">
        <strong>About wait stats.</strong> {% if report.snapshot %}PostgreSQL waits are a point-in-time snapshot of currently-waiting
        sessions (<code>pg_stat_activity</code> + ungranted <code>pg_locks</code>), not cumulative totals — run repeatedly to spot
        patterns.{% else %}Figures are cumulative since the stats last reset (a restart clears them). Benign/idle waits are filtered out.{% endif %}
        The AI analysis (when enabled) receives only wait-type names and percentages — never any table data.
    </div>
</body>
</html>
"""


def build_waits_json(target: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "waits",
        "target": target,
        "dialect": report.dialect,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": report.snapshot,
        "unit": report.unit,
        "summary": summarize(report),
        "category_totals": report.category_totals,
        "waits": [asdict(w) for w in report.waits],
        "ai_explanation": report.ai_explanation,
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_waits_html(target, report, output_path):
    template = Environment(autoescape=True).from_string(WAITS_TEMPLATE)
    html = template.render(
        target=target,
        report=report,
        summary=summarize(report),
        cat_colors=_CAT_COLORS,
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wait-statistics report written to {output_path}")
