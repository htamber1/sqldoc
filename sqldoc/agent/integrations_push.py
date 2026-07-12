"""Scheduled auto-push of documentation to the configured integrations.

When ``agent.integrations`` names one or more report connectors (SharePoint,
Confluence, Notion, Google Drive, Box, Azure DevOps), the daemon runs this
module every ``push_interval_hours`` (default 24). For each monitored database it
collects one bundle and pushes the rendered reports/metrics to each connector.

The cadence is enforced with a per-connector timestamp in the store's kv table
(so a daemon restart doesn't re-push, and each connector paces independently).
Every push is best-effort and isolated: one failing connector or database is
recorded as an event, never a crash.
"""
import time

from sqldoc.integrations import get_client, section
from sqldoc.integrations.reports import gather, render_artifacts, metrics as bundle_metrics


def _adapter_for(db_config):
    from sqldoc.adapters import get_adapter
    return get_adapter(db_config.connection_string, db_config.dialect)


def _meta_key(name: str) -> str:
    return f"last_integration_push:{name}"


def _due(store, name: str, interval_seconds: float, now: float) -> bool:
    raw = store.get_meta(_meta_key(name))
    if not raw:
        return True
    try:
        return (now - float(raw)) >= interval_seconds
    except (TypeError, ValueError):
        return True


def push_once(agent_config, store, log=print) -> list:
    """Push reports for every database to every configured connector, ignoring
    the schedule. Returns a list of per-(connector, db) result dicts."""
    cfg = agent_config.raw_config or {}
    results = []
    for name in agent_config.integrations:
        try:
            conf = section(cfg, name)
            client = get_client(name, conf)
        except Exception as e:
            log(f"integration '{name}' config error: {type(e).__name__}: {e}")
            store.add_event("(integrations)", "integration_push",
                            f"{name}: config error", {"error": str(e)})
            results.append({"integration": name, "db": None, "ok": False, "error": str(e)})
            continue
        for db in agent_config.databases:
            try:
                adapter = _adapter_for(db)
                bundle = gather(adapter, db.name)
                artifacts = render_artifacts(bundle)
                res = client.push_reports(artifacts, metrics=bundle_metrics(bundle))
                log(f"[{db.name}] pushed to {name}: {res.get('detail')}")
                store.add_event(db.name, "integration_push",
                                f"pushed to {name}", {"detail": res.get("detail")})
                results.append({"integration": name, "db": db.name, "ok": True,
                                "detail": res.get("detail")})
            except Exception as e:
                log(f"[{db.name}] push to {name} FAILED: {type(e).__name__}: {e}")
                store.add_event(db.name, "integration_push",
                                f"push to {name} failed", {"error": str(e)})
                results.append({"integration": name, "db": db.name, "ok": False,
                                "error": str(e)})
    return results


def maybe_push(agent_config, store, log=print, now=None) -> list:
    """Push to each connector whose interval has elapsed; update its timestamp."""
    if not agent_config.integrations:
        return []
    now = time.time() if now is None else now
    interval = max(1.0, float(agent_config.push_interval_hours)) * 3600.0
    results = []
    for name in agent_config.integrations:
        if not _due(store, name, interval, now):
            continue
        # Push this connector for all databases, then stamp its timestamp.
        one = [r for r in push_once(_only(agent_config, name), store, log)]
        results.extend(one)
        store.set_meta(_meta_key(name), str(now))
    return results


def _only(agent_config, name):
    """Shallow view of the config scoped to a single integration name."""
    import copy
    scoped = copy.copy(agent_config)
    scoped.integrations = [name]
    return scoped
