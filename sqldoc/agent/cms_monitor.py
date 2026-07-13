"""CMS-driven agent monitoring.

When the agent is configured with a CMS server it monitors every registered
server. This module discovers the inventory, turns it into per-server
:class:`DatabaseConfig` s, and reconciles the live CMS registration against what's
currently monitored — detecting servers added to or removed from the CMS, and
flagging servers that have become unreachable.
"""
from sqldoc.agent.config import DatabaseConfig


def discover(cms_cfg: dict):
    """Discover the CMS inventory (module-level indirection for mocking)."""
    from sqldoc.cms import discover_live
    return discover_live(cms_cfg["server"], windows_auth=cms_cfg.get("windows_auth", True),
                         username=cms_cfg.get("username"), password=cms_cfg.get("password"))


def build_databases(inventory, cms_cfg: dict) -> list:
    """One DatabaseConfig per registered server (Windows auth by default)."""
    from sqldoc.cms import connection_string_for
    db = cms_cfg.get("database", "master")
    out = []
    for s in inventory.servers:
        cs = connection_string_for(s.server_name, database=db,
                                   windows_auth=cms_cfg.get("windows_auth", True),
                                   username=cms_cfg.get("username"), password=cms_cfg.get("password"))
        out.append(DatabaseConfig(name=s.name, connection_string=cs, dialect="sqlserver",
                                  mode=cms_cfg.get("mode", "local"),
                                  no_ai=bool(cms_cfg.get("no_ai", True))))
    return out


def reconcile(current_names, inventory):
    """Return (added_servers, removed_names) comparing monitored names to the CMS."""
    inv_names = {s.name for s in inventory.servers}
    current = set(current_names)
    added = [s for s in inventory.servers if s.name not in current]
    removed = sorted(current - inv_names)
    return added, removed


def probe(db_config) -> bool:
    """True if the server is reachable (module-level for mocking)."""
    try:
        from sqldoc.adapters import get_adapter
        conn = get_adapter(db_config.connection_string, db_config.dialect).connect()
        conn.close()
        return True
    except Exception:
        return False


def reconcile_once(store, cms_cfg, notifier, monitored_names, start_fn, stop_fn,
                   log=print, discover_fn=None, probe_reachability=True) -> dict:
    """One reconciliation pass: discover the CMS, start monitoring newly-added
    servers, stop removed ones, and alert on unreachable servers. Returns a
    changes summary. Best-effort — a discovery failure is logged, not raised."""
    changes = {"added": [], "removed": [], "unreachable": []}
    try:
        inv = discover_fn() if discover_fn else discover(cms_cfg)
    except Exception as e:
        log(f"cms reconcile: discovery failed: {type(e).__name__}: {e}")
        return changes

    added, removed = reconcile(monitored_names, inv)
    dbs = build_databases(inv, cms_cfg)
    by_name = {d.name: d for d in dbs}

    for s in added:
        db = by_name.get(s.name)
        if db is None:
            continue
        start_fn(db)
        changes["added"].append(s.name)
        store.add_event("(cms)", "cms_server_added", f"CMS server added: {s.name}",
                        {"host": s.server_name, "group": s.group_path})
        _notify(notifier, "cms_server_added", f"CMS server added: {s.name}",
                f"Now monitoring {s.name} ({s.server_name}).")

    for name in removed:
        stop_fn(name)
        changes["removed"].append(name)
        store.add_event("(cms)", "cms_server_removed", f"CMS server removed: {name}", None)
        _notify(notifier, "cms_server_removed", f"CMS server removed: {name}",
                f"Stopped monitoring {name} (no longer registered in the CMS).")

    if probe_reachability:
        for db in dbs:
            if not probe(db):
                changes["unreachable"].append(db.name)
                store.add_event(db.name, "cms_server_unreachable",
                                f"{db.name} is unreachable", None)
                _notify(notifier, "cms_server_unreachable", f"{db.name} unreachable",
                        f"The CMS-registered server {db.name} could not be reached.")

    return changes


def _notify(notifier, event_type, title, text):
    if notifier is None:
        return
    try:
        notifier.notify(event_type, title, text)
    except Exception:
        pass
