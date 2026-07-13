"""Daily estate-wide change digest — one email covering what changed across every
monitored server overnight: new tables, dropped columns, new stored procedures,
and new PII findings. Built from the schema-change / new-PII events the agent
already records; idempotent per calendar day."""
import json
from datetime import datetime, timedelta, timezone

from sqldoc.agent import notify as notify_mod

_LAST_SENT_KEY = "last_estate_digest"
_CHANGE_TYPES = ("schema_change", "new_pii", "cms_server_added", "cms_server_removed")


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def _day_key(dt) -> str:
    return dt.strftime("%Y-%m-%d")


def _parse_detail(detail):
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str):
        try:
            return json.loads(detail)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _extract_schema_change(diff) -> dict:
    """Structured counts from a stored schema-change diff."""
    d = _parse_detail(diff)
    new_cols = dropped_cols = 0
    for tm in d.get("tables_modified", []) or []:
        new_cols += len(tm.get("added", []))
        dropped_cols += len(tm.get("removed", []))
    return {
        "new_tables": d.get("tables_added", []) or [],
        "dropped_tables": d.get("tables_removed", []) or [],
        "new_columns": new_cols, "dropped_columns": dropped_cols,
        "new_procedures": d.get("procedures_added", []) or [],
        "dropped_procedures": d.get("procedures_removed", []) or [],
    }


def collect_estate_changes(store, since_iso: str) -> dict:
    """{server: {events, schema, headlines}} for every change since since_iso."""
    events = [e for e in store.events_since(since_iso) if e.get("type") in _CHANGE_TYPES]
    by_server = {}
    for e in events:
        srv = e.get("db_name") or "(estate)"
        bucket = by_server.setdefault(srv, {"schema_changes": [], "new_pii": [], "other": []})
        if e["type"] == "schema_change":
            bucket["schema_changes"].append({"at": e["at"], "summary": e["summary"],
                                             **_extract_schema_change(e.get("detail"))})
        elif e["type"] == "new_pii":
            bucket["new_pii"].append({"at": e["at"], "summary": e["summary"]})
        else:
            bucket["other"].append({"at": e["at"], "type": e["type"], "summary": e["summary"]})
    return by_server


def estate_totals(by_server: dict) -> dict:
    t = {"servers_changed": 0, "new_tables": 0, "dropped_tables": 0,
         "new_columns": 0, "dropped_columns": 0, "new_procedures": 0,
         "dropped_procedures": 0, "new_pii": 0}
    for srv, b in by_server.items():
        if b["schema_changes"] or b["new_pii"] or b["other"]:
            t["servers_changed"] += 1
        for sc in b["schema_changes"]:
            t["new_tables"] += len(sc["new_tables"])
            t["dropped_tables"] += len(sc["dropped_tables"])
            t["new_columns"] += sc["new_columns"]
            t["dropped_columns"] += sc["dropped_columns"]
            t["new_procedures"] += len(sc["new_procedures"])
            t["dropped_procedures"] += len(sc["dropped_procedures"])
        t["new_pii"] += len(b["new_pii"])
    return t


# --- render ----------------------------------------------------------------

def render_estate_digest_html(by_server, totals, day_label) -> str:
    import html as _h
    rows = []
    for srv in sorted(by_server):
        b = by_server[srv]
        if not (b["schema_changes"] or b["new_pii"] or b["other"]):
            continue
        items = []
        for sc in b["schema_changes"]:
            bits = []
            if sc["new_tables"]:
                bits.append(f"+{len(sc['new_tables'])} table(s): {', '.join(sc['new_tables'][:8])}")
            if sc["dropped_tables"]:
                bits.append(f"-{len(sc['dropped_tables'])} table(s)")
            if sc["new_columns"]:
                bits.append(f"+{sc['new_columns']} column(s)")
            if sc["dropped_columns"]:
                bits.append(f"-{sc['dropped_columns']} column(s)")
            if sc["new_procedures"]:
                bits.append(f"+{len(sc['new_procedures'])} proc(s)")
            if sc["dropped_procedures"]:
                bits.append(f"-{len(sc['dropped_procedures'])} proc(s)")
            items.append("<li>" + _h.escape("; ".join(bits) or sc["summary"]) + "</li>")
        for p in b["new_pii"]:
            items.append(f"<li>PII: {_h.escape(p['summary'])}</li>")
        for o in b["other"]:
            items.append(f"<li>{_h.escape(o['type'])}: {_h.escape(o['summary'])}</li>")
        rows.append(f"<h3 style='margin:14px 0 4px'>{_h.escape(srv)}</h3><ul>{''.join(items)}</ul>")

    summary = (f"<p><strong>{totals['servers_changed']}</strong> server(s) changed overnight: "
               f"+{totals['new_tables']} tables, -{totals['dropped_tables']} tables, "
               f"+{totals['new_columns']} / -{totals['dropped_columns']} columns, "
               f"+{totals['new_procedures']} procedures, {totals['new_pii']} new PII finding(s).</p>")
    body = summary + ("".join(rows) if rows else "<p>No changes across the estate.</p>")
    return (f"<html><body style='font-family:-apple-system,Segoe UI,sans-serif;color:#222'>"
            f"<h2>sqldoc estate change digest &mdash; {_h.escape(day_label)}</h2>{body}"
            f"<p style='color:#888;font-size:12px'>Generated by the sqldoc agent.</p></body></html>")


def render_estate_digest_text(by_server, totals, day_label) -> str:
    lines = [f"sqldoc estate change digest - {day_label}",
             f"{totals['servers_changed']} server(s) changed: +{totals['new_tables']} tables, "
             f"-{totals['dropped_tables']} tables, +{totals['new_columns']}/-{totals['dropped_columns']} columns, "
             f"+{totals['new_procedures']} procs, {totals['new_pii']} new PII", ""]
    for srv in sorted(by_server):
        b = by_server[srv]
        if not (b["schema_changes"] or b["new_pii"] or b["other"]):
            continue
        lines.append(f"[{srv}]")
        for sc in b["schema_changes"]:
            lines.append(f"  - {sc['summary']}")
        for p in b["new_pii"]:
            lines.append(f"  - PII: {p['summary']}")
    return "\n".join(lines) + "\n"


# --- scheduling ------------------------------------------------------------

def is_due(cfg, now, last_key) -> bool:
    return cfg.enabled and now.hour >= cfg.hour and last_key != _day_key(now)


def maybe_send_estate_digest(store, agent_config, now=None, log=lambda *_: None,
                             send_fn=None) -> bool:
    cfg = getattr(agent_config, "estate_digest", None)
    if not cfg or not cfg.enabled:
        return False
    smtp = getattr(agent_config.notify, "smtp", None)
    if not smtp:
        return False
    now = now or datetime.now()
    last = store.get_meta(_LAST_SENT_KEY)
    if not is_due(cfg, now, last):
        return False

    since_iso = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    by_server = collect_estate_changes(store, since_iso)
    totals = estate_totals(by_server)
    day_label = now.strftime("%B %d, %Y")
    html = render_estate_digest_html(by_server, totals, day_label)
    text = render_estate_digest_text(by_server, totals, day_label)
    subject = f"[sqldoc] Estate change digest - {now.strftime('%b %d, %Y')}"

    send = send_fn or notify_mod.send_html_email
    try:
        send(smtp, subject, html, text)
        store.set_meta(_LAST_SENT_KEY, _day_key(now))
        log(f"estate digest emailed ({totals['servers_changed']} server(s) changed)")
        return True
    except Exception as e:
        log(f"estate digest failed: {type(e).__name__}: {e}")
        return False
