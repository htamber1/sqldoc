"""HTML + JSON rendering for the `sqldoc secure` report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.secure import summarize

SECURE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ target }} — Security Scan</title>
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
        .container { max-width: 1100px; margin: 0 auto; padding: 36px 20px 20px; }
        .scoreband { display: flex; align-items: center; gap: 28px; background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 26px 30px; margin-bottom: 26px; flex-wrap: wrap; }
        .gauge { position: relative; width: 130px; height: 130px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
        .gauge .inner { position: absolute; inset: 12px; background: var(--card); border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        .gauge .val { font-size: 2.3rem; font-weight: 800; }
        .gauge .grade { font-size: 0.78rem; color: var(--muted); letter-spacing: 0.1em; }
        .sev-summary { display: flex; gap: 14px; flex-wrap: wrap; }
        .sev-box { text-align: center; padding: 12px 20px; border-radius: 12px; border: 1px solid var(--border); min-width: 96px; }
        .sev-box .n { font-size: 1.7rem; font-weight: 800; }
        .sev-box .l { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-top: 2px; }
        .sev-box.HIGH .n { color: var(--red); } .sev-box.MEDIUM .n { color: var(--amber); } .sev-box.LOW .n { color: var(--blue); }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 26px 0 12px; }
        .finding { background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--border); border-radius: 12px; padding: 16px 20px; margin-bottom: 12px; }
        .finding.HIGH { border-left-color: var(--red); }
        .finding.MEDIUM { border-left-color: var(--amber); }
        .finding.LOW { border-left-color: var(--blue); }
        .finding .top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .sev { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 0.68rem; font-weight: 700; }
        .sev.HIGH { background: rgba(220,38,38,0.15); color: var(--red); }
        .sev.MEDIUM { background: rgba(245,158,11,0.15); color: var(--amber); }
        .sev.LOW { background: rgba(96,165,250,0.15); color: var(--blue); }
        .cat { font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
        .finding .title { font-weight: 700; color: var(--text-strong); font-size: 0.98rem; }
        .finding .detail { color: #cbd5e1; font-size: 0.86rem; margin-top: 6px; line-height: 1.5; }
        .finding .rec { color: var(--green); font-size: 0.84rem; margin-top: 6px; }
        .finding .rec::before { content: "\\2192 "; }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; color: var(--amber); font-size: 0.83rem; }
        .clean { text-align: center; color: var(--green); padding: 30px; font-size: 1rem; font-weight: 600; }
        .footer { max-width: 1100px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Security Scan</span>
        <h1>{{ target }}</h1>
        <p>Generated on {{ generated_at }} &middot; {{ report.dialect }} hardening checks &middot; {{ summary.checks_run }} checks run</p>
    </div>
    <div class="container">
        <div class="scoreband">
            <div class="gauge" style="background: conic-gradient({{ gauge_color }} {{ summary.score * 3.6 }}deg, #2a3340 0);">
                <div class="inner"><div class="val" style="color: {{ gauge_color }};">{{ summary.score }}</div><div class="grade">GRADE {{ summary.grade }}</div></div>
            </div>
            <div>
                <div style="font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 10px;">Security score (0-100)</div>
                <div class="sev-summary">
                    <div class="sev-box HIGH"><div class="n">{{ summary.high }}</div><div class="l">High</div></div>
                    <div class="sev-box MEDIUM"><div class="n">{{ summary.medium }}</div><div class="l">Medium</div></div>
                    <div class="sev-box LOW"><div class="n">{{ summary.low }}</div><div class="l">Low</div></div>
                </div>
            </div>
        </div>

        {% if report.errors %}
        <div class="warn">Some checks could not run (permissions?):
            {% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }}</div>{% endfor %}
        </div>
        {% endif %}

        <h2 class="section">Findings</h2>
        {% for f in report.findings %}
        <div class="finding {{ f.severity }}">
            <div class="top">
                <span class="sev {{ f.severity }}">{{ f.severity }}</span>
                <span class="cat">{{ f.category }}</span>
                <span class="title">{{ f.title }}</span>
            </div>
            {% if f.detail %}<div class="detail">{{ f.detail }}</div>{% endif %}
            {% if f.recommendation %}<div class="rec">{{ f.recommendation }}</div>{% endif %}
        </div>
        {% endfor %}
        {% if not report.findings %}<div class="clean">No security issues detected by these checks. Keep verifying — this is not a full audit.</div>{% endif %}
    </div>
    <div class="footer">
        <strong>Scope.</strong> These are heuristic hardening checks against server configuration and catalog metadata, not a full
        penetration test or compliance audit. The 0-100 score deducts 15 per HIGH, 7 per MEDIUM, and 3 per LOW finding. No table row data
        was read. Always validate findings against your environment before acting.
    </div>
</body>
</html>
"""

_GAUGE_COLORS = {"A": "#34d399", "B": "#34d399", "C": "#fbbf24", "D": "#fb923c", "F": "#f87171"}


def build_secure_json(target: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "security",
        "target": target,
        "dialect": report.dialect,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(report),
        "findings": [asdict(f) for f in report.findings],
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_secure_html(target, report, output_path):
    s = summarize(report)
    template = Environment(autoescape=True).from_string(SECURE_TEMPLATE)
    html = template.render(
        target=target,
        report=report,
        summary=s,
        gauge_color=_GAUGE_COLORS.get(s["grade"], "#94a3b8"),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Security report written to {output_path}")
