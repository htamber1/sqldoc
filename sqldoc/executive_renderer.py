"""HTML rendering for the `sqldoc executive` single-page summary.

Fully self-contained (no external CSS/JS/fonts/images) so it passes the
air-gap check, and deliberately plain-English — this is the one report a
non-technical executive reads.
"""
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__

EXECUTIVE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ s.database }} — Executive Summary</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --bg: #0a0a0f; --card: #1e2530; --text: #e5e7eb; --text-strong: #f8fafc;
            --muted: #94a3b8; --faint: #64748b; --border: #2a3340;
            --red: #f87171; --amber: #fbbf24; --green: #34d399; --blue: #60a5fa;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; }
        .header { background: radial-gradient(900px 300px at 85% -30%, rgba(96,165,250,0.14), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 48px 40px 40px; border-bottom: 1px solid var(--border); }
        .header .brand { font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 10px; }
        .header h1 { font-size: 2rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); }
        .header p { color: var(--muted); font-size: 0.9rem; margin-top: 6px; }
        .container { max-width: 1000px; margin: 0 auto; padding: 34px 20px 60px; }
        .overall { display: flex; align-items: center; gap: 30px; background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 30px 34px; margin-bottom: 28px; flex-wrap: wrap; }
        .gauge { position: relative; width: 150px; height: 150px; border-radius: 50%; flex-shrink: 0;
                 background: conic-gradient({{ overall_color }} calc({{ s.overall_score }} * 1%), #2a3340 0); display: flex; align-items: center; justify-content: center; }
        .gauge .inner { position: absolute; inset: 13px; background: var(--card); border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        .gauge .val { font-size: 2.6rem; font-weight: 800; color: var(--text-strong); }
        .gauge .of { font-size: 0.7rem; color: var(--faint); }
        .overall .say h2 { font-size: 1.5rem; color: var(--text-strong); font-weight: 800; }
        .overall .say p { color: var(--muted); font-size: 0.95rem; margin-top: 8px; max-width: 520px; line-height: 1.5; }
        .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin-bottom: 30px; }
        .metric { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 22px 24px; }
        .metric .label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
        .metric .row { display: flex; align-items: baseline; gap: 10px; margin-top: 10px; }
        .metric .num { font-size: 2.1rem; font-weight: 800; }
        .metric .unit { font-size: 0.9rem; color: var(--faint); }
        .metric .verdict { font-size: 0.86rem; margin-top: 6px; }
        .metric.na .num { color: var(--faint); }
        .trend { font-size: 0.82rem; margin-top: 10px; display: inline-flex; align-items: center; gap: 5px; }
        .trend.better { color: var(--green); }
        .trend.worse { color: var(--red); }
        .trend.flat { color: var(--faint); }
        h2.section { font-size: 1.2rem; font-weight: 700; color: var(--text-strong); margin: 8px 0 16px; }
        .risk { display: flex; gap: 16px; background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--border); border-radius: 12px; padding: 18px 22px; margin-bottom: 14px; }
        .risk.Critical { border-left-color: var(--red); }
        .risk.High { border-left-color: var(--amber); }
        .risk.Medium { border-left-color: var(--blue); }
        .risk .rank { font-size: 1.6rem; font-weight: 800; color: var(--faint); min-width: 30px; }
        .risk .title { font-weight: 700; color: var(--text-strong); font-size: 1rem; }
        .risk .badge { display: inline-block; margin-left: 8px; padding: 1px 9px; border-radius: 20px; font-size: 0.66rem; font-weight: 700; vertical-align: middle; }
        .risk.Critical .badge { background: rgba(220,38,38,0.15); color: var(--red); }
        .risk.High .badge { background: rgba(245,158,11,0.15); color: var(--amber); }
        .risk.Medium .badge { background: rgba(96,165,250,0.15); color: var(--blue); }
        .risk .detail { color: #cbd5e1; font-size: 0.9rem; margin-top: 6px; line-height: 1.5; }
        .allclear { background: rgba(52,211,153,0.08); border: 1px solid rgba(52,211,153,0.3); border-radius: 12px; padding: 22px 26px; color: var(--green); font-size: 0.95rem; }
        .footer { color: var(--faint); font-size: 0.78rem; margin-top: 40px; text-align: center; line-height: 1.7; }
    </style>
</head>
<body>
    <div class="header">
        <div class="brand">sqldoc &middot; Executive Summary</div>
        <h1>{{ s.database }}</h1>
        <p>A plain-English health &amp; risk overview for leadership. {{ s.generated_label }}</p>
    </div>
    <div class="container">
        <div class="overall">
            <div class="gauge"><div class="inner"><div class="val">{{ s.overall_score }}</div><div class="of">out of 100</div></div></div>
            <div class="say">
                <h2>{{ s.overall_label }}</h2>
                <p>{{ overall_sentence }}</p>
            </div>
        </div>

        <div class="cards">
            {% for c in cards %}
            <div class="metric {{ 'na' if c.value is none else '' }}">
                <div class="label">{{ c.label }}</div>
                <div class="row"><span class="num" style="color: {{ c.color }}">{{ c.display }}</span><span class="unit">{{ c.unit }}</span></div>
                <div class="verdict" style="color: {{ c.color }}">{{ c.verdict }}</div>
                {% if c.trend %}<div class="trend {{ c.trend.cls }}">{{ c.trend.arrow }} {{ c.trend.text }}</div>{% endif %}
            </div>
            {% endfor %}
        </div>

        <h2 class="section">Top priorities</h2>
        {% if s.top_risks %}
            {% for r in s.top_risks %}
            <div class="risk {{ r.severity }}">
                <div class="rank">{{ loop.index }}</div>
                <div>
                    <div class="title">{{ r.title }}<span class="badge">{{ r.severity }}</span></div>
                    <div class="detail">{{ r.detail }}</div>
                </div>
            </div>
            {% endfor %}
        {% else %}
            <div class="allclear">No urgent issues were found across data protection, backups, security, and performance. Keep monitoring on a regular schedule.</div>
        {% endif %}

        <div class="footer">
            Generated by sqldoc v{{ version }} &middot; {{ generated_at }}<br>
            Scores combine data-protection, backup, security, and performance checks. Higher is better; the PII figure shown is a safety score.
        </div>
    </div>
</body>
</html>
"""


_ARROWS = {"up": "▲", "down": "▼", "flat": "▬"}


def _color(score):
    if score is None:
        return "#64748b"
    if score >= 75:
        return "#34d399"
    if score >= 50:
        return "#fbbf24"
    return "#f87171"


def _verdict(score):
    if score is None:
        return "Not available for this database type"
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 60:
        return "Fair"
    if score >= 40:
        return "Needs attention"
    return "Urgent"


def _trend_view(summary, metric, lower_is_better=False):
    t = summary.trends.get(metric)
    if not t:
        return None
    if t["direction"] == "flat":
        return {"cls": "flat", "arrow": _ARROWS["flat"], "text": "No change since last run"}
    cls = "better" if t["better"] else "worse"
    word = "improved" if t["better"] else "worse"
    return {"cls": cls, "arrow": _ARROWS[t["direction"]],
            "text": f"{abs(t['delta'])} point{'s' if abs(t['delta']) != 1 else ''} {word} vs last run"}


def _overall_sentence(summary):
    n = len(summary.top_risks)
    if summary.overall_score >= 90 and n == 0:
        return "This database is in excellent shape. Data is well protected, backups are current, and security is strong."
    if n == 0:
        return "No urgent problems stand out, but there is room to strengthen the areas below."
    return (f"There {'is 1 area' if n == 1 else f'are {n} areas'} that need attention. "
            "The most important items are listed under Top priorities below.")


def build_executive_json(summary) -> dict:
    from sqldoc.executive import build_executive_json as _bej
    return _bej(summary)


def render_executive_html(summary, output_path):
    ps = summary.pii_safety_score
    cards = [
        {"label": "Data protection", "value": ps,
         "display": "N/A" if ps is None else str(ps), "unit": "" if ps is None else "/ 100",
         "color": _color(ps), "verdict": _verdict(ps),
         "trend": _trend_view(summary, "pii_risk_score")},
        {"label": "Backups", "value": summary.backup_compliance_pct,
         "display": "N/A" if summary.backup_compliance_pct is None else str(summary.backup_compliance_pct),
         "unit": "" if summary.backup_compliance_pct is None else "% covered",
         "color": _color(summary.backup_compliance_pct), "verdict": _verdict(summary.backup_compliance_pct),
         "trend": _trend_view(summary, "backup_compliance_pct")},
        {"label": "Security", "value": summary.security_score,
         "display": "N/A" if summary.security_score is None else str(summary.security_score),
         "unit": "" if summary.security_score is None else f"/ 100  ({summary.security_grade})",
         "color": _color(summary.security_score), "verdict": _verdict(summary.security_score),
         "trend": _trend_view(summary, "security_score")},
        {"label": "Performance", "value": summary.health_score,
         "display": "N/A" if summary.health_score is None else str(summary.health_score),
         "unit": "" if summary.health_score is None else "/ 100",
         "color": _color(summary.health_score), "verdict": _verdict(summary.health_score),
         "trend": _trend_view(summary, "health_score")},
    ]
    template = Environment(autoescape=True).from_string(EXECUTIVE_TEMPLATE)
    html = template.render(
        s=summary, cards=cards,
        overall_color=_color(summary.overall_score),
        overall_sentence=_overall_sentence(summary),
        version=__version__,
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Executive summary written to {output_path}")
