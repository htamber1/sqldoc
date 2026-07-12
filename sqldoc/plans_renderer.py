"""HTML + JSON rendering for the `sqldoc plans` report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.plans import summarize

_PAT_COLORS = {
    "missing-index": "#f87171", "table-scan": "#f87171", "large-scan": "#fbbf24",
    "key-lookup": "#fbbf24", "implicit-conversion": "#fb923c", "spill": "#c084fc",
    "no-index": "#f87171",
}

PLANS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ target }} — Query Plans</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(96,165,250,0.13), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--blue), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1150px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 20px; text-align: center; }
        .stat-card .number { font-size: 1.9rem; font-weight: 800; }
        .stat-card .label { color: var(--muted); font-size: 0.72rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); } .c-blue .number { color: var(--blue); }
        .plan { background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--border); border-radius: 12px; margin-bottom: 16px; overflow: hidden; }
        .plan.HIGH { border-left-color: var(--red); }
        .plan.MEDIUM { border-left-color: var(--amber); }
        .plan .phead { padding: 14px 20px; background: var(--card-head); display: flex; gap: 14px; align-items: center; flex-wrap: wrap; border-bottom: 1px solid var(--border-strong); }
        .plan .rank { font-weight: 800; color: var(--faint); font-size: 1.1rem; }
        .plan .metric { font-family: 'Consolas', monospace; font-size: 0.82rem; }
        .plan .metric b { color: var(--text-strong); }
        .pats { display: flex; gap: 6px; flex-wrap: wrap; margin-left: auto; }
        .pat { display: inline-block; padding: 2px 9px; border-radius: 5px; font-size: 0.66rem; font-weight: 700; }
        .body { padding: 16px 20px; }
        .sql { font-family: 'Consolas', monospace; font-size: 0.8rem; color: #cbd5e1; white-space: pre-wrap; word-break: break-word; background: #0c1119; border-radius: 8px; padding: 12px 14px; max-height: 200px; overflow-y: auto; }
        .patlist { margin-top: 12px; }
        .patrow { font-size: 0.83rem; color: #cbd5e1; padding: 3px 0; }
        .patrow .sev { font-weight: 700; }
        .ai { margin-top: 12px; background: linear-gradient(180deg, #202a38, #1a222e); border: 1px solid rgba(96,165,250,0.3); border-radius: 10px; padding: 14px 16px; }
        .ai h4 { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--blue); margin-bottom: 8px; }
        .ai .txt { color: #dbe4ee; font-size: 0.86rem; line-height: 1.55; white-space: pre-wrap; }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .note { background: rgba(148,163,184,0.08); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--muted); font-size: 0.83rem; }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .footer { max-width: 1150px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Query Plans</span>
        <h1>{{ target }}</h1>
        <p>Generated on {{ generated_at }} &middot; {{ report.dialect }} &middot; top {{ summary.plans }} worst cached queries{% if summary.has_plan_xml %} with execution-plan analysis{% endif %}</p>
    </div>
    <div class="container">
        {% if report.errors %}
        <div class="warn">{% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }}</div>{% endfor %}</div>
        {% endif %}
        {% for n in report.notes %}<div class="note">{{ n }}</div>{% endfor %}

        <div class="stats">
            <div class="stat-card c-blue"><div class="number">{{ summary.plans }}</div><div class="label">Plans analyzed</div></div>
            <div class="stat-card {{ 'c-red' if summary.high_severity else 'c-amber' }}"><div class="number">{{ summary.high_severity }}</div><div class="label">High-severity plans</div></div>
            <div class="stat-card c-amber"><div class="number">{{ '{:,.0f}'.format(summary.worst_ms) }}</div><div class="label">Worst avg (ms)</div></div>
        </div>

        {% for plan in report.plans %}
        <div class="plan {{ plan.severity }}">
            <div class="phead">
                <span class="rank">#{{ loop.index }}</span>
                <span class="metric"><b>{{ '{:,.1f}'.format(plan.avg_elapsed_ms) }}</b> ms avg</span>
                <span class="metric">&times;<b>{{ '{:,}'.format(plan.executions) }}</b> execs</span>
                <span class="metric"><b>{{ '{:,.0f}'.format(plan.total_elapsed_ms) }}</b> ms total</span>
                <span class="pats">
                    {% for p in plan.patterns %}<span class="pat" style="background: {{ pat_colors.get(p.kind, '#64748b') }}22; color: {{ pat_colors.get(p.kind, '#64748b') }};">{{ p.kind }}{% if p.count > 1 %} x{{ p.count }}{% endif %}</span>{% endfor %}
                </span>
            </div>
            <div class="body">
                <div class="sql">{{ plan.query_text }}</div>
                {% if plan.patterns %}
                <div class="patlist">
                    {% for p in plan.patterns %}<div class="patrow"><span class="sev" style="color: {{ pat_colors.get(p.kind, '#64748b') }};">{{ p.severity }}</span> — {{ p.detail }}</div>{% endfor %}
                </div>
                {% endif %}
                {% if plan.ai_explanation %}
                <div class="ai"><h4>AI recommendation</h4><div class="txt">{{ plan.ai_explanation }}</div></div>
                {% endif %}
            </div>
        </div>
        {% endfor %}
        {% if not report.plans %}<div class="empty">No cached query statistics available.</div>{% endif %}
    </div>
    <div class="footer">
        <strong>About plan analysis.</strong> Queries come from the engine's statement statistics; SQL Server execution plans are parsed
        for anti-patterns. Figures are cumulative since the stats last reset. The AI recommendation (when enabled) receives the query text
        and detected patterns — never table row data. Always test suggested indexes before creating them.
    </div>
</body>
</html>
"""


def build_plans_json(target: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "plans",
        "target": target,
        "dialect": report.dialect,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(report),
        "plans": [{
            "query_text": p.query_text, "avg_elapsed_ms": p.avg_elapsed_ms,
            "executions": p.executions, "total_elapsed_ms": p.total_elapsed_ms,
            "avg_reads": p.avg_reads, "severity": p.severity,
            "patterns": [asdict(pat) for pat in p.patterns],
            "ai_explanation": p.ai_explanation,
        } for p in report.plans],
        "notes": report.notes,
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_plans_html(target, report, output_path):
    template = Environment(autoescape=True).from_string(PLANS_TEMPLATE)
    html = template.render(
        target=target,
        report=report,
        summary=summarize(report),
        pat_colors=_PAT_COLORS,
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Query-plans report written to {output_path}")
