"""Renderers for the CMS inventory: a console tree, a self-contained HTML tree,
and machine-readable JSON."""
import html

_CSS = """
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:24px}
header{border-bottom:1px solid #21262d;padding:16px 24px;background:#161b22}
h1{margin:0;font-size:19px}.sub{color:#8b949e;font-size:12px}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin:14px 0}
.grp{font-weight:600;color:#e6edf3;margin:10px 0 4px}
ul{list-style:none;margin:0;padding-left:18px;border-left:1px solid #21262d}
li{padding:2px 0}
.srv{color:#58a6ff;font-family:ui-monospace,Consolas,monospace}
.host{color:#8b949e;font-size:12px}
.desc{color:#8b949e;font-size:12px;font-style:italic}
.muted{color:#8b949e}.pill{display:inline-block;background:#21262d;border-radius:10px;padding:1px 8px;font-size:11px}
"""


def _e(x):
    return html.escape("" if x is None else str(x))


def build_inventory_json(inv) -> dict:
    from sqldoc.cms import to_config
    d = to_config(inv)
    d["report_type"] = "cms-inventory"
    d["server_count"] = len(inv.servers)
    d["group_count"] = len([g for g in inv.groups if not g.is_system])
    return d


def _tree(inv):
    """Nested {group_id: {'group': CmsGroup, 'children': [ids], 'servers': [CmsServer]}}."""
    by_parent = {}
    for g in inv.groups:
        by_parent.setdefault(g.parent_id, []).append(g)
    servers_by_group = {}
    for s in inv.servers:
        servers_by_group.setdefault(s.group_id, []).append(s)
    return by_parent, servers_by_group


def render_tree_text(inv) -> str:
    by_parent, servers_by_group = _tree(inv)
    lines = []

    def walk(group, depth):
        indent = "  " * depth
        if not group.is_system:
            lines.append(f"{indent}[{group.name}]" + (f"  - {group.description}" if group.description else ""))
            depth_children = depth + 1
        else:
            depth_children = depth
        for s in sorted(servers_by_group.get(group.id, []), key=lambda x: x.name):
            si = "  " * depth_children
            extra = f"  ({s.server_name})" if s.server_name != s.name else ""
            lines.append(f"{si}- {s.name}{extra}" + (f"  {s.description}" if s.description else ""))
        for child in sorted(by_parent.get(group.id, []), key=lambda x: x.name):
            walk(child, depth_children)

    roots = by_parent.get(None, []) + [g for g in inv.groups if g.parent_id is not None
                                       and g.parent_id not in {x.id for x in inv.groups}]
    for root in roots:
        walk(root, 0)
    # servers with no/unknown group
    orphan = [s for s in inv.servers if s.group_id not in {g.id for g in inv.groups}]
    for s in sorted(orphan, key=lambda x: x.name):
        lines.append(f"- {s.name}  ({s.server_name})")
    return "\n".join(lines)


def render_tree_html(inv, output_path):
    by_parent, servers_by_group = _tree(inv)

    def walk(group):
        parts = []
        if not group.is_system:
            parts.append(f"<div class='grp'>&#128193; {_e(group.name)}"
                         + (f" <span class='desc'>{_e(group.description)}</span>" if group.description else "")
                         + "</div>")
        parts.append("<ul>")
        for s in sorted(servers_by_group.get(group.id, []), key=lambda x: x.name):
            host = f" <span class='host'>({_e(s.server_name)})</span>" if s.server_name != s.name else ""
            desc = f" <span class='desc'>{_e(s.description)}</span>" if s.description else ""
            parts.append(f"<li>&#128421; <span class='srv'>{_e(s.name)}</span>{host}{desc}</li>")
        for child in sorted(by_parent.get(group.id, []), key=lambda x: x.name):
            parts.append("<li>" + walk(child) + "</li>")
        parts.append("</ul>")
        return "".join(parts)

    known = {g.id for g in inv.groups}
    roots = by_parent.get(None, [])
    body_tree = "".join(walk(r) for r in sorted(roots, key=lambda x: x.name))
    orphan = [s for s in inv.servers if s.group_id not in known]
    if orphan:
        body_tree += "<div class='grp'>Ungrouped</div><ul>" + "".join(
            f"<li>&#128421; <span class='srv'>{_e(s.name)}</span> "
            f"<span class='host'>({_e(s.server_name)})</span></li>"
            for s in sorted(orphan, key=lambda x: x.name)) + "</ul>"

    ngroups = len([g for g in inv.groups if not g.is_system])
    head = (f"<div class='card'><span class='pill'>{len(inv.servers)} servers</span> "
            f"<span class='pill'>{ngroups} groups</span> "
            f"<span class='muted'>CMS: {_e(inv.cms_server)} &middot; discovered {_e(inv.discovered_at)}</span></div>")
    doc = (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>CMS inventory - {_e(inv.cms_server)}</title><style>{_CSS}</style></head>"
           f"<body><header><h1>&#127970; Central Management Server inventory</h1>"
           f"<span class='sub'>sqldoc cms</span></header>"
           f"<div class='wrap'>{head}<div class='card'>{body_tree}</div></div></body></html>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(doc)
