"""Estate-wide access audit — `sqldoc access review --cms`.

Reads server-level principals (logins + fixed server-role memberships) from every
CMS-registered server in parallel, then surfaces the cross-server findings a
compliance team needs:

* **elevated_multi** — principals with an elevated server role (sysadmin /
  securityadmin / serveradmin / setupadmin) on more than one server;
* **coverage_gaps** — principals that exist on some servers but not all (drift);
* **orphaned** — Windows logins with no backing AD object (needs an AD source).
"""
from dataclasses import dataclass, field
import html

ELEVATED_ROLES = {"sysadmin", "securityadmin", "serveradmin", "setupadmin"}


@dataclass
class EstateAccessReport:
    servers: list = field(default_factory=list)          # server names probed (ok)
    failed: list = field(default_factory=list)           # (server, error)
    elevated_multi: list = field(default_factory=list)   # {principal, servers, roles}
    coverage_gaps: list = field(default_factory=list)    # {principal, present_on, missing_from}
    orphaned: list = field(default_factory=list)         # {principal, servers}


def _login_worker(server, opts):
    from sqldoc.cms_bulk import _adapter_for
    from sqldoc.access.sqlserver import collect_server_logins
    adapter = _adapter_for(server, opts, database="master")
    conn = adapter.connect()
    try:
        logins = collect_server_logins(adapter.cursor(conn))
        return {"logins": [{"name": l.name, "type": l.type, "is_disabled": l.is_disabled,
                            "server_roles": l.server_roles} for l in logins]}
    finally:
        conn.close()


def _is_system(name: str) -> bool:
    n = (name or "").upper()
    return (n.startswith("##") or n.startswith("NT AUTHORITY\\")
            or n.startswith("NT SERVICE\\") or n in ("PUBLIC",))


def aggregate(results, source=None) -> EstateAccessReport:
    rep = EstateAccessReport()
    logins_by_server = {}
    for r in results:
        if r.ok:
            rep.servers.append(r.server)
            logins_by_server[r.server] = r.summary.get("logins", [])
        else:
            rep.failed.append((r.server, r.error))

    all_servers = set(rep.servers)

    # principal -> {server: login-dict}
    by_principal = {}
    for srv, logins in logins_by_server.items():
        for lg in logins:
            by_principal.setdefault(lg["name"], {})[srv] = lg

    for name, per_server in sorted(by_principal.items()):
        if _is_system(name):
            continue
        # elevated on multiple servers
        elevated_on = {srv: [r for r in lg["server_roles"] if r in ELEVATED_ROLES]
                       for srv, lg in per_server.items()}
        elevated_servers = {srv for srv, roles in elevated_on.items() if roles}
        if len(elevated_servers) >= 2:
            roles = sorted({r for srv in elevated_servers for r in elevated_on[srv]})
            rep.elevated_multi.append({"principal": name,
                                       "servers": sorted(elevated_servers), "roles": roles})
        # exists on some servers but not all
        present = set(per_server)
        missing = all_servers - present
        if present and missing:
            rep.coverage_gaps.append({"principal": name, "present_on": sorted(present),
                                      "missing_from": sorted(missing)})
        # orphaned Windows logins (no backing AD object)
        if source is not None and any("WINDOWS_LOGIN" in (lg["type"] or "").upper()
                                      for lg in per_server.values()):
            part = name.split("\\")[-1]
            try:
                u = source.get_user(part)
                if not u.found:
                    rep.orphaned.append({"principal": name, "servers": sorted(present)})
            except Exception:
                pass

    rep.elevated_multi.sort(key=lambda x: -len(x["servers"]))
    rep.coverage_gaps.sort(key=lambda x: -len(x["missing_from"]))
    return rep


def collect_estate_access(inventory, opts, source=None, group=None, max_workers=8) -> EstateAccessReport:
    from sqldoc.cms_bulk import run_against_servers
    from sqldoc.cms import select_servers
    servers = select_servers(inventory, group)
    results = run_against_servers(servers, _login_worker, opts, max_workers)
    return aggregate(results, source=source)


# --- render ----------------------------------------------------------------

def build_estate_access_json(rep: EstateAccessReport) -> dict:
    return {
        "report_type": "cms-access-review",
        "servers_audited": len(rep.servers),
        "unreachable": [{"server": s, "error": e} for s, e in rep.failed],
        "elevated_on_multiple": rep.elevated_multi,
        "coverage_gaps": rep.coverage_gaps,
        "orphaned": rep.orphaned,
    }


def _e(x):
    return html.escape("" if x is None else str(x))


def render_estate_access_html(rep: EstateAccessReport, output_path, cms_server=""):
    css = ("body{background:#0d1117;color:#c9d1d9;font:14px/1.5 -apple-system,Segoe UI,sans-serif;"
           "margin:0}.wrap{max-width:1050px;margin:0 auto;padding:24px}"
           "header{background:#161b22;border-bottom:1px solid #21262d;padding:16px 24px}"
           "h1{margin:0;font-size:19px}h2{font-size:15px;margin:22px 0 8px}"
           ".card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin:12px 0}"
           "table{border-collapse:collapse;width:100%;font-size:13px}"
           "th,td{border:1px solid #21262d;padding:6px 9px;text-align:left}th{background:#0d1117;color:#8b949e}"
           ".mono{font-family:ui-monospace,Consolas,monospace}.muted{color:#8b949e}"
           ".pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}"
           ".hi{background:#3d1a1d;color:#f85149}.med{background:#3a2d12;color:#d29922}")

    def section(title, rows, headers, empty):
        if not rows:
            return f"<h2>{_e(title)}</h2><div class='card'><p class='muted'>{_e(empty)}</p></div>"
        head = "".join(f"<th>{_e(h)}</th>" for h in headers)
        return (f"<h2>{_e(title)}</h2><div class='card'><table><tr>{head}</tr>"
                + "".join(rows) + "</table></div>")

    em = [f"<tr><td class='mono'>{_e(x['principal'])}</td>"
          f"<td><span class='pill hi'>{len(x['servers'])} servers</span></td>"
          f"<td>{_e(', '.join(x['servers']))}</td><td>{_e(', '.join(x['roles']))}</td></tr>"
          for x in rep.elevated_multi]
    cg = [f"<tr><td class='mono'>{_e(x['principal'])}</td>"
          f"<td>{_e(', '.join(x['present_on']))}</td>"
          f"<td><span class='pill med'>{_e(', '.join(x['missing_from']))}</span></td></tr>"
          for x in rep.coverage_gaps]
    orph = [f"<tr><td class='mono'>{_e(x['principal'])}</td><td>{_e(', '.join(x['servers']))}</td></tr>"
            for x in rep.orphaned]

    head = (f"<div class='card'><span class='pill'>{len(rep.servers)} servers audited</span> "
            + (f"<span class='pill hi'>{len(rep.failed)} unreachable</span>" if rep.failed else "")
            + "</div>")
    body = (head
            + section("Elevated on multiple servers", em,
                      ["Principal", "Reach", "Servers", "Roles"],
                      "No principal holds an elevated server role on more than one server.")
            + section("Coverage gaps (exists on some servers, not others)", cg,
                      ["Principal", "Present on", "Missing from"],
                      "No coverage gaps — principals are consistent across the estate.")
            + section("Orphaned Windows logins (no AD backing)", orph,
                      ["Principal", "Servers"],
                      "No orphaned logins detected (or no AD source configured)."))
    doc = (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Estate access review</title><style>{css}</style></head>"
           f"<body><header><h1>&#127970; Estate-wide access review</h1></header>"
           f"<div class='wrap'>{body}</div></body></html>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(doc)
