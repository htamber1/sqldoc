"""Board-level HTML for the CMS executive estate dashboard (air-gap safe)."""
import html

_CSS = """
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
header{border-bottom:1px solid #21262d;padding:16px 24px;background:#161b22}
h1{margin:0;font-size:20px}h2{font-size:15px;margin:22px 0 10px}.sub{color:#8b949e;font-size:12px}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin:14px 0}
.scores{display:flex;gap:14px;flex-wrap:wrap}
.score{flex:1;min-width:150px;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:14px;text-align:center}
.score .n{font-size:30px;font-weight:700}.score .l{color:#8b949e;font-size:12px;text-transform:uppercase}
.good{color:#3fb950}.warn{color:#d29922}.bad{color:#f85149}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #21262d;padding:6px 9px;text-align:left}th{background:#0d1117;color:#8b949e}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.Critical{background:#3d1a1d;color:#f85149}.High{background:#3a2d12;color:#d29922}
.Medium{background:#132a3f;color:#58a6ff}.Low{background:#21262d;color:#8b949e}
.muted{color:#8b949e}.host{color:#8b949e;font-size:11px}.err{color:#f85149}
"""


def _e(x):
    return html.escape("" if x is None else str(x))


def _color(v, invert=False):
    if v is None:
        return "muted"
    good, bad = (80, 50)
    if invert:
        return "bad" if v >= good else ("warn" if v >= bad else "good")
    return "good" if v >= good else ("warn" if v >= bad else "bad")


def _score_card(label, value, unit=""):
    disp = "N/A" if value is None else f"{value}{unit}"
    return (f"<div class='score'><div class='n {_color(value)}'>{_e(disp)}</div>"
            f"<div class='l'>{_e(label)}</div></div>")


def render_estate_html(estate, output_path, cms_server=""):
    ok = [r for r in estate.results if r.ok]
    scorecard = ("<div class='card'><div class='scores'>"
                 + _score_card("Overall", estate.overall)
                 + _score_card("Data protection", estate.pii_safety)
                 + _score_card("Backups", estate.backup, "%")
                 + _score_card("Security", estate.security)
                 + _score_card("Performance", estate.health)
                 + "</div></div>")

    summary = (f"<div class='card'><h2 style='margin-top:0'>Estate summary</h2>"
               f"<p><strong>{estate.server_count}</strong> server(s) &middot; "
               f"<strong>{estate.database_count}</strong> user database(s) &middot; "
               f"<strong>{estate.pii_total}</strong> sensitive column(s) across the estate"
               + (f" &middot; <span class='err'>{len(estate.failed)} unreachable</span>"
                  if estate.failed else "")
               + "</p></div>")

    # top risks
    if estate.top_risks:
        rows = "".join(
            f"<tr><td>{i}</td><td><span class='pill {_e(r.get('severity'))}'>{_e(r.get('severity'))}</span></td>"
            f"<td>{_e(r.get('title'))}</td><td class='muted'>{_e(r.get('server'))}</td></tr>"
            for i, r in enumerate(estate.top_risks, 1))
        risks = ("<h2>Top risks across the estate</h2><div class='card'><table>"
                 "<tr><th>#</th><th>Severity</th><th>Risk</th><th>Server</th></tr>"
                 + rows + "</table></div>")
    else:
        risks = "<div class='card'><p class='good'>No urgent risks across the estate.</p></div>"

    # per-server scores
    srv_rows = []
    for r in sorted(estate.results, key=lambda x: (x.group, x.server)):
        loc = f"{_e(r.server)}<div class='host'>{_e(r.host)}</div>"
        if r.ok:
            s = r.summary
            srv_rows.append(
                f"<tr><td>{loc}</td><td class='muted'>{_e(r.group)}</td>"
                f"<td class='{_color(s.get('overall'))}'>{_e(s.get('overall'))}</td>"
                f"<td class='{_color(s.get('pii_safety'))}'>{_e(s.get('pii_safety'))}</td>"
                f"<td class='{_color(s.get('backup_pct'))}'>{_e(s.get('backup_pct'))}</td>"
                f"<td class='{_color(s.get('security'))}'>{_e(s.get('security'))} "
                f"({_e(s.get('security_grade'))})</td>"
                f"<td class='{_color(s.get('health'))}'>{_e(s.get('health'))}</td>"
                f"<td>{_e(s.get('pii_findings'))}</td></tr>")
        else:
            srv_rows.append(f"<tr><td>{loc}</td><td class='muted'>{_e(r.group)}</td>"
                            f"<td colspan='6' class='err'>unreachable: {_e(r.error)}</td></tr>")
    per_server = ("<h2>Per-server scorecard</h2><div class='card'><table>"
                  "<tr><th>Server</th><th>Group</th><th>Overall</th><th>Data prot.</th>"
                  "<th>Backups</th><th>Security</th><th>Perf.</th><th>PII cols</th></tr>"
                  + "".join(srv_rows) + "</table></div>")

    doc = (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Estate executive summary</title><style>{_CSS}</style></head>"
           f"<body><header><h1>&#127970; SQL Server estate &mdash; executive summary</h1>"
           f"<span class='sub'>sqldoc executive --cms &middot; {_e(cms_server)}</span></header>"
           f"<div class='wrap'>{scorecard}{summary}{risks}{per_server}</div></body></html>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(doc)
