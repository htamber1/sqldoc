"""HTML + JSON rendering for the `sqldoc logs` ERRORLOG report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.logs import summarize

LOGS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ server_name }} — Error Log</title>
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
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(248,113,113,0.13), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--red), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1280px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 20px; text-align: center; }
        .stat-card .number { font-size: 2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.72rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); }
        .c-blue .number { color: var(--blue); } .c-green .number { color: var(--green); }
        .filters { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
        .chip { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 0.76rem; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--muted); }
        .cat { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }
        .catbox { padding: 8px 14px; border-radius: 10px; font-size: 0.8rem; font-weight: 700; border: 1px solid; }
        .catbox.corruption { background: rgba(220,38,38,0.13); color: var(--red); border-color: rgba(220,38,38,0.4); }
        .catbox.deadlock { background: rgba(251,146,60,0.13); color: var(--orange); border-color: rgba(251,146,60,0.4); }
        .catbox.memory-pressure { background: rgba(192,132,252,0.13); color: var(--violet); border-color: rgba(192,132,252,0.4); }
        .catbox.disk-full { background: rgba(245,158,11,0.13); color: var(--amber); border-color: rgba(245,158,11,0.4); }
        .catbox.login-failure { background: rgba(96,165,250,0.13); color: var(--blue); border-color: rgba(96,165,250,0.4); }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 10px 14px; text-align: left; font-size: 0.7rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 9px 14px; font-size: 0.82rem; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr.critical td { background: rgba(220,38,38,0.06); }
        .mono { font-family: 'Consolas', monospace; white-space: nowrap; }
        .txt { font-family: 'Consolas', monospace; font-size: 0.78rem; color: #cbd5e1; white-space: pre-wrap; word-break: break-word; }
        .sev { display: inline-block; min-width: 24px; text-align: center; padding: 2px 7px; border-radius: 5px; font-size: 0.7rem; font-weight: 700; font-family: 'Consolas', monospace; }
        .sev.hi { background: rgba(220,38,38,0.15); color: var(--red); }
        .sev.mid { background: rgba(245,158,11,0.15); color: var(--amber); }
        .sev.lo { background: rgba(148,163,184,0.12); color: var(--muted); }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.66rem; font-weight: 700; }
        .badge.corruption { background: rgba(220,38,38,0.15); color: var(--red); }
        .badge.deadlock { background: rgba(251,146,60,0.15); color: var(--orange); }
        .badge.memory-pressure { background: rgba(192,132,252,0.15); color: var(--violet); }
        .badge.disk-full { background: rgba(245,158,11,0.15); color: var(--amber); }
        .badge.login-failure { background: rgba(96,165,250,0.15); color: var(--blue); }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .empty { text-align: center; color: var(--faint); padding: 26px; font-size: 0.85rem; }
        .footer { max-width: 1280px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Error Log</span>
        <h1>{{ server_name }}</h1>
        <p>Generated on {{ generated_at }} &middot; {{ report.source }} via sys.xp_readerrorlog</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-blue"><div class="number">{{ summary.entries }}</div><div class="label">Entries</div></div>
            <div class="stat-card {{ 'c-red' if summary.critical else 'c-green' }}"><div class="number">{{ summary.critical }}</div><div class="label">Critical</div></div>
            <div class="stat-card {{ 'c-red' if summary.high_severity else 'c-green' }}"><div class="number">{{ summary.high_severity }}</div><div class="label">Severity 17+</div></div>
            <div class="stat-card c-amber"><div class="number">{{ summary.max_severity }}</div><div class="label">Max severity</div></div>
        </div>

        <div class="filters">
            {% if report.search %}<span class="chip">search: {{ report.search }}</span>{% endif %}
            {% if report.severity_filter is not none %}<span class="chip">severity &ge; {{ report.severity_filter }}</span>{% endif %}
            {% if report.last_hours %}<span class="chip">last {{ report.last_hours }}h</span>{% endif %}
        </div>

        {% if summary.by_category %}
        <div class="cat">
            {% for cat, n in summary.by_category.items() %}<div class="catbox {{ cat }}">{{ cat }} &middot; {{ n }}</div>{% endfor %}
        </div>
        {% endif %}

        {% if report.errors %}
        <div class="warn">{% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }}</div>{% endfor %}</div>
        {% endif %}

        <div class="panel">
            <table>
                <thead><tr><th>Time</th><th>Source</th><th>Sev</th><th>Category</th><th>Message</th></tr></thead>
                <tbody>
                    {% for e in report.entries %}
                    <tr class="{{ 'critical' if e.critical else '' }}">
                        <td class="mono">{{ e.log_date }}</td>
                        <td class="mono">{{ e.process_info }}</td>
                        <td>{% if e.severity is not none %}<span class="sev {{ 'hi' if e.severity >= 17 else ('mid' if e.severity >= 11 else 'lo') }}">{{ e.severity }}</span>{% else %}<span class="sev lo">—</span>{% endif %}</td>
                        <td>{% if e.critical %}<span class="badge {{ e.critical }}">{{ e.critical }}</span>{% endif %}</td>
                        <td class="txt">{{ e.text }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.entries %}<div class="empty">No matching error-log entries.</div>{% endif %}
        </div>
    </div>
    <div class="footer">
        <strong>Method.</strong> Entries come from <code>sys.xp_readerrorlog</code>. Severity and error numbers are parsed from the message
        text; the critical categories (corruption, deadlock, memory pressure, disk-full, login failure) are matched against well-known
        patterns and error numbers. No table row data was read.
    </div>
</body>
</html>
"""


def build_logs_json(server_name: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "logs",
        "server": server_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": report.source,
        "filters": {"search": report.search, "severity": report.severity_filter,
                    "last_hours": report.last_hours},
        "summary": summarize(report),
        "entries": [asdict(e) for e in report.entries],
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_logs_html(server_name, report, output_path):
    template = Environment(autoescape=True).from_string(LOGS_TEMPLATE)
    html = template.render(
        server_name=server_name,
        report=report,
        summary=summarize(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Error-log report written to {output_path}")
