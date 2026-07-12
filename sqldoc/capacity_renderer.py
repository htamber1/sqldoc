"""HTML + JSON rendering for the `sqldoc capacity` report (SVG sparklines)."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.capacity import summarize


def sparkline(series, width=200, height=40, pad=3):
    """Build an SVG polyline `points` string from a numeric series. Accepts a
    list of numbers or a list of (label, value) pairs."""
    vals = []
    for item in series:
        v = item[1] if isinstance(item, (list, tuple)) else item
        if v is not None:
            vals.append(v)
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + i * (width - 2 * pad) / (n - 1)
        y = height - pad - (v - lo) / rng * (height - 2 * pad)
        pts.append(f"{round(x, 1)},{round(y, 1)}")
    return " ".join(pts)


def _days_label(days):
    if days is None:
        return "no projection"
    if days > 3650:
        return ">10 years"
    return f"{days:g} days"


CAPACITY_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Capacity Planning</title>
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
        .container { max-width: 1150px; margin: 0 auto; padding: 36px 20px 20px; }
        h2.db { font-size: 1.25rem; font-weight: 800; color: var(--text-strong); margin: 26px 0 6px; font-family: 'Consolas', monospace; }
        .sub { color: var(--muted); font-size: 0.83rem; margin-bottom: 14px; }
        .proj-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 18px; }
        .proj { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px; }
        .proj .name { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
        .proj .val { font-size: 1.5rem; font-weight: 800; color: var(--text-strong); margin: 4px 0; }
        .proj .rate { font-size: 0.82rem; color: var(--muted); font-family: 'Consolas', monospace; }
        .proj .until { margin-top: 8px; font-size: 0.86rem; font-weight: 700; }
        .until.warn { color: var(--amber); } .until.bad { color: var(--red); } .until.ok { color: var(--green); }
        .spark { margin-top: 10px; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; margin-bottom: 8px; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 10px 16px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 9px 16px; font-size: 0.85rem; border-bottom: 1px solid var(--border); }
        tr:last-child td { border-bottom: none; }
        .mono { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .num { text-align: right; font-family: 'Consolas', monospace; }
        .notice { background: rgba(148,163,184,0.08); border: 1px solid var(--border); border-radius: 12px; padding: 20px; color: var(--muted); font-size: 0.9rem; margin-bottom: 16px; }
        .footer { max-width: 1150px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Capacity Planning</span>
        <h1>Capacity &amp; growth projections</h1>
        <p>Generated on {{ generated_at }} &middot; from the agent's recorded metric history</p>
    </div>
    <div class="container">
        {% for rep in reports %}
        <h2 class="db">{{ rep.database }}</h2>
        {% if not rep.sufficient %}
        <div class="notice">{% for n in rep.notes %}{{ n }}{% endfor %}</div>
        {% else %}
        <div class="sub">{{ rep.points }} data points over {{ rep.span_days }} day(s)</div>
        <div class="proj-grid">
            {% for p in [rep.disk, rep.db_size, rep.fragmentation] if p %}
            <div class="proj">
                <div class="name">{{ p.metric|replace('_', ' ') }}</div>
                <div class="val">{{ '{:,.1f}'.format(p.current) }} {{ p.unit }}</div>
                <div class="rate">{{ '%+.2f'|format(p.rate_per_day) }} {{ p.unit }}/day{% if p.limit %} &middot; limit {{ '{:,.0f}'.format(p.limit) }} {{ p.unit }}{% endif %}</div>
                {% if p.metric == 'disk_free' %}
                <div class="until {{ 'bad' if p.days_until_limit is not none and p.days_until_limit < 30 else ('warn' if p.days_until_limit is not none else 'ok') }}">Disk full in: {{ days_label(p.days_until_limit) }}</div>
                {% elif p.metric == 'database_size' %}
                <div class="until {{ 'bad' if p.days_until_limit is not none and p.days_until_limit < 30 else ('warn' if p.days_until_limit is not none else 'ok') }}">Reaches max size in: {{ days_label(p.days_until_limit) }}</div>
                {% else %}
                <div class="until {{ 'warn' if p.rate_per_day > 0 else 'ok' }}">Trend: {{ 'rising' if p.rate_per_day > 0 else 'flat/falling' }}</div>
                {% endif %}
                {% set pts = spark(p.history) %}
                {% if pts %}<svg class="spark" width="200" height="40" viewBox="0 0 200 40" xmlns="http://www.w3.org/2000/svg"><polyline points="{{ pts }}" fill="none" stroke="{{ '#f87171' if p.metric != 'disk_free' else '#60a5fa' }}" stroke-width="2"/></svg>{% endif %}
            </div>
            {% endfor %}
        </div>

        {% if rep.table_growth %}
        <div class="panel">
            <table>
                <thead><tr><th>Table</th><th>Current (MB)</th><th>Growth/day (MB)</th><th>+30d</th><th>+60d</th><th>+90d</th></tr></thead>
                <tbody>
                    {% for g in rep.table_growth %}
                    <tr>
                        <td class="mono">{{ g.obj }}</td>
                        <td class="num">{{ '{:,.1f}'.format(g.current_mb) }}</td>
                        <td class="num">{{ '%+.3f'|format(g.rate_mb_per_day) }}</td>
                        <td class="num">{{ '{:,.1f}'.format(g.size_30d) }}</td>
                        <td class="num">{{ '{:,.1f}'.format(g.size_60d) }}</td>
                        <td class="num">{{ '{:,.1f}'.format(g.size_90d) }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
        {% endif %}
        {% endfor %}
        {% if not reports %}<div class="notice">No monitored databases with history were found in the agent store. Run <code>sqldoc agent start</code> and let it complete at least two polling cycles.</div>{% endif %}
    </div>
    <div class="footer">
        <strong>About these projections.</strong> Trends are a simple linear rate over the recorded history — accuracy improves with more
        data points and a longer window, and real growth is rarely perfectly linear. "Days until" figures assume the current rate
        continues. SQL Server provides disk + max-size + fragmentation; PostgreSQL/MySQL provide database + table sizes.
    </div>
</body>
</html>
"""


def build_capacity_json(reports) -> dict:
    def rep_json(r):
        return {
            "database": r.database, "points": r.points, "span_days": r.span_days,
            "sufficient": r.sufficient, "summary": summarize(r),
            "disk": asdict(r.disk) if r.disk else None,
            "db_size": asdict(r.db_size) if r.db_size else None,
            "fragmentation": asdict(r.fragmentation) if r.fragmentation else None,
            "table_growth": [asdict(g) for g in r.table_growth],
            "notes": r.notes,
        }
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "capacity",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "databases": [rep_json(r) for r in reports],
    }


def render_capacity_html(reports, output_path):
    template = Environment(autoescape=True).from_string(CAPACITY_TEMPLATE)
    html = template.render(
        reports=reports,
        spark=sparkline,
        days_label=_days_label,
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Capacity report written to {output_path}")
