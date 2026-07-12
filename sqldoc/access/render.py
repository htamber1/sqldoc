"""HTML + JSON renderers for the access suite. Self-contained dark-theme HTML
(air-gap safe: all CSS inline, no external refs)."""
import html
import json

_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
header{border-bottom:1px solid #21262d;padding:16px 24px;background:#161b22}
h1{margin:0;font-size:19px}h2{font-size:15px;margin:22px 0 10px;color:#e6edf3}
.sub{color:#8b949e;font-size:12px}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:13px;overflow-x:auto;display:block}
th,td{border:1px solid #21262d;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#0d1117;color:#8b949e;font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.read{background:#132a3f;color:#58a6ff}.write{background:#3a2d12;color:#d29922}
.admin{background:#3d1a1d;color:#f85149}.none{background:#21262d;color:#8b949e}
.HIGH{background:#3d1a1d;color:#f85149}.MEDIUM{background:#3a2d12;color:#d29922}.LOW{background:#132a3f;color:#58a6ff}
.ALREADY{color:#3fb950}.PARTIAL{color:#d29922}.NONE{color:#f85149}
.muted{color:#8b949e}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
pre{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:12px;overflow-x:auto;font-size:12.5px}
ul{margin:6px 0;padding-left:20px}
.tag{display:inline-block;background:#21262d;border-radius:4px;padding:1px 6px;margin:1px;font-size:11px}
"""


def _doc(title, body) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><header><h1>{html.escape(title)}</h1>"
            f"<span class='sub'>sqldoc access</span></header>"
            f"<div class='wrap'>{body}</div></body></html>")


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _level_pill(level) -> str:
    return f"<span class='pill {_e(level)}'>{_e(level)}</span>"


def _write(output_path, text):
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)


# --- check -----------------------------------------------------------------

def build_check_json(report) -> dict:
    u = report.user
    return {
        "report_type": "access-check",
        "user": {
            "identifier": u.identifier, "display_name": u.display_name,
            "sam_account_name": u.sam_account_name, "email": u.email,
            "login": u.login, "title": u.title, "department": u.department,
            "enabled": u.enabled, "source": u.source, "found": u.found,
            "groups": u.groups,
        },
        "matched_groups": report.matched_groups,
        "logins": [{"name": l.name, "type": l.type, "server": l.server,
                    "is_disabled": l.is_disabled, "server_roles": l.server_roles}
                   for l in report.logins],
        "access": [{
            "server": a.server, "database": a.database, "login": a.login,
            "db_user": a.db_user, "via": a.via, "roles": a.roles, "level": a.level,
            "permissions": [{"permission": p[0], "state": p[1], "schema": p[2], "object": p[3]}
                            for p in a.permissions],
            "pii_tables": [{"schema": s, "table": t, "risk": r, "regulations": g}
                           for (s, t, r, g) in a.pii_tables],
        } for a in report.access],
        "errors": [{"where": w, "message": m} for (w, m) in report.errors],
    }


def render_check_html(report, output_path):
    u = report.user
    if not u.found:
        _write(output_path, _doc("Access check",
                f"<div class='card'><p class='NONE'>User '{_e(u.identifier)}' not found in "
                f"{_e(u.source or 'AD')}.</p></div>"))
        return

    groups = "".join(f"<span class='tag'>{_e(g)}</span>" for g in u.groups) or "<span class='muted'>none</span>"
    matched = "".join(f"<span class='tag'>{_e(g)}</span>" for g in report.matched_groups) or "<span class='muted'>none</span>"
    header = (
        "<div class='card'>"
        f"<h2 style='margin-top:0'>{_e(u.display_name or u.identifier)}</h2>"
        f"<div class='muted'>{_e(u.login or u.sam_account_name)} &middot; {_e(u.email)}"
        f" &middot; {_e(u.title or '?')}, {_e(u.department or '?')} &middot; source: {_e(u.source)}"
        f" &middot; {'enabled' if u.enabled else 'DISABLED'}</div>"
        f"<p><strong>AD groups ({len(u.groups)}):</strong> {groups}</p>"
        f"<p><strong>Groups with SQL Server access:</strong> {matched}</p></div>")

    if report.access:
        rows = []
        for a in report.access:
            roles = "".join(f"<span class='tag'>{_e(r)}</span>" for r in a.roles) or "<span class='muted'>-</span>"
            pii = (f"<span class='pill {_e(a.pii_tables[0][2])}'>{len(a.pii_tables)} PII table(s)</span>"
                   if a.pii_tables else "<span class='muted'>none</span>")
            rows.append(
                f"<tr><td>{_e(a.server)}</td><td>{_e(a.database)}</td>"
                f"<td class='mono'>{_e(a.login)}</td><td>{_level_pill(a.level)}</td>"
                f"<td>{roles}</td><td>{_e(len(a.permissions))} grant(s)</td><td>{pii}</td></tr>")
        access = ("<h2>Current access by database</h2><div class='card'><table>"
                  "<tr><th>Server</th><th>Database</th><th>Login</th><th>Level</th>"
                  "<th>Roles</th><th>Explicit grants</th><th>PII exposure</th></tr>"
                  + "".join(rows) + "</table></div>")
        # PII detail
        pii_rows = []
        for a in report.access:
            for (s, t, r, g) in a.pii_tables:
                pii_rows.append(f"<tr><td>{_e(a.database)}</td><td class='mono'>{_e(s)}.{_e(t)}</td>"
                                f"<td><span class='pill {_e(r)}'>{_e(r)}</span></td>"
                                f"<td>{_e(', '.join(g))}</td></tr>")
        if pii_rows:
            access += ("<h2>PII-flagged tables currently accessible</h2><div class='card'><table>"
                       "<tr><th>Database</th><th>Table</th><th>Risk</th><th>Regulations</th></tr>"
                       + "".join(pii_rows) + "</table></div>")
    else:
        access = "<div class='card'><p class='muted'>No SQL Server access found for this user.</p></div>"

    errs = ""
    if report.errors:
        items = "".join(f"<li>{_e(w)}: {_e(m)}</li>" for (w, m) in report.errors)
        errs = f"<div class='card'><h2 style='margin-top:0'>Notes</h2><ul class='muted'>{items}</ul></div>"

    _write(output_path, _doc(f"Access check — {u.display_name or u.identifier}",
                             header + access + errs))
