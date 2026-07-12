"""HTML + JSON rendering for the `sqldoc baseline` comparison report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.baseline import summarize

BASELINE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ target }} — Performance Baseline</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(251,146,60,0.12), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, #fb923c, transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1100px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 20px; text-align: center; }
        .stat-card .number { font-size: 1.9rem; font-weight: 800; }
        .stat-card .label { color: var(--muted); font-size: 0.72rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); } .c-green .number { color: var(--green); } .c-blue .number { color: var(--blue); }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 20px 0 12px; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 16px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 10px 16px; font-size: 0.85rem; border-bottom: 1px solid var(--border); }
        tr:last-child td { border-bottom: none; }
        .mono { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .num { text-align: right; font-family: 'Consolas', monospace; }
        .chg { font-weight: 700; color: var(--red); }
        .kind { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.66rem; font-weight: 700; background: rgba(96,165,250,0.14); color: var(--blue); }
        .kind.query { background: rgba(192,132,252,0.14); color: var(--violet); }
        .sql { font-family: 'Consolas', monospace; font-size: 0.78rem; color: #cbd5e1; white-space: pre-wrap; word-break: break-word; max-width: 420px; }
        .clean { text-align: center; color: var(--green); padding: 34px; font-size: 1.05rem; font-weight: 600; }
        .footer { max-width: 1100px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Performance Baseline</span>
        <h1>{{ target }}</h1>
        <p>Generated on {{ generated_at }} &middot; {{ report.dialect }} &middot; baseline {{ report.baseline_at }} vs current {{ report.current_at }} &middot; threshold {{ report.threshold_pct }}%</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card {{ 'c-red' if summary.anomalies else 'c-green' }}"><div class="number">{{ summary.anomalies }}</div><div class="label">Regressions</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.metric_regressions }}</div><div class="label">Metric</div></div>
            <div class="stat-card c-violet"><div class="number">{{ summary.query_regressions }}</div><div class="label">Query</div></div>
            <div class="stat-card c-blue"><div class="number">{{ summary.metrics_compared }}</div><div class="label">Metrics compared</div></div>
        </div>

        {% if report.anomalies %}
        <h2 class="section">Regressions (worse than {{ report.threshold_pct }}% vs baseline)</h2>
        <div class="panel">
            <table>
                <thead><tr><th>Metric</th><th>Type</th><th>Baseline</th><th>Current</th><th>Change</th><th>Detail</th></tr></thead>
                <tbody>
                    {% for a in report.anomalies %}
                    <tr>
                        <td class="mono">{{ a.metric }}</td>
                        <td><span class="kind {{ a.kind }}">{{ a.kind }}</span></td>
                        <td class="num">{{ '{:,.1f}'.format(a.baseline) }}</td>
                        <td class="num">{{ '{:,.1f}'.format(a.current) }}</td>
                        <td class="num chg">+{{ a.change_pct }}%</td>
                        <td class="sql">{{ a.detail }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <div class="clean">No performance regressions beyond the {{ report.threshold_pct }}% threshold. Performance is at or better than the baseline.</div>
        {% endif %}
    </div>
    <div class="footer">
        <strong>About baselines.</strong> Metrics are cumulative statistics ("higher is worse"): wait time, connection counts, query
        average durations, and job durations. Cumulative counters grow between captures, so capture the baseline and compare over
        comparable windows (ideally after a stats reset). Only regressions above the threshold are shown. No table row data was read.
    </div>
</body>
</html>
"""


def build_baseline_json(target: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "baseline",
        "target": target,
        "dialect": report.dialect,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_at": report.baseline_at,
        "current_at": report.current_at,
        "threshold_pct": report.threshold_pct,
        "summary": summarize(report),
        "anomalies": [asdict(a) for a in report.anomalies],
    }


def render_baseline_html(target, report, output_path):
    template = Environment(autoescape=True).from_string(BASELINE_TEMPLATE)
    html = template.render(
        target=target,
        report=report,
        summary=summarize(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Baseline comparison report written to {output_path}")
