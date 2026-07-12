"""Self-contained (air-gap safe) HTML for the compliance-framework assessment."""
import html

_CSS = """
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:24px}
header{border-bottom:1px solid #21262d;padding:16px 24px;background:#161b22}
h1{margin:0;font-size:19px}h2{font-size:15px;margin:20px 0 8px}
.sub{color:#8b949e;font-size:12px}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #21262d;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#0d1117;color:#8b949e}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.attention{background:#3d1a1d;color:#f85149}.review{background:#3a2d12;color:#d29922}.pass{background:#12261a;color:#3fb950}
.muted{color:#8b949e}.mono{font-family:ui-monospace,Consolas,monospace}
"""


def _e(x):
    return html.escape("" if x is None else str(x))


def render_frameworks_html(results, database, output_path):
    cards = []
    for r in results:
        s = r.summary
        rows = []
        for c in r.controls:
            items = ("<br>".join(_e(i) for i in c.findings[:15])
                     + ("<br><span class='muted'>…</span>" if len(c.findings) > 15 else "")) or "<span class='muted'>-</span>"
            rows.append(
                f"<tr><td class='mono'>{_e(c.control_id)}</td><td>{_e(c.title)}</td>"
                f"<td><span class='pill {_e(c.status)}'>{_e(c.status)}</span></td>"
                f"<td>{_e(c.detail)}</td><td>{items}</td></tr>")
        cards.append(
            f"<div class='card'><h2 style='margin-top:0'>{_e(r.name)}</h2>"
            f"<p><span class='pill attention'>{s['attention']} attention</span> "
            f"<span class='pill review'>{s['review']} review</span> "
            f"<span class='pill pass'>{s['pass']} pass</span></p>"
            "<table><tr><th>Control</th><th>Requirement</th><th>Status</th>"
            "<th>Assessment</th><th>Findings</th></tr>" + "".join(rows) + "</table></div>")
    body = (f"<div class='card'><p class='muted'>Framework control assessment for "
            f"<strong>{_e(database)}</strong>. A mapping aid based on schema + access "
            f"metadata — not a certification.</p></div>" + "".join(cards))
    doc = (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Compliance frameworks — {_e(database)}</title><style>{_CSS}</style></head>"
           f"<body><header><h1>Compliance frameworks</h1>"
           f"<span class='sub'>sqldoc comply</span></header>"
           f"<div class='wrap'>{body}</div></body></html>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(doc)
