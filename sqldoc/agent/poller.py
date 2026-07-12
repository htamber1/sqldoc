"""One monitoring pass over a single database.

`poll_database` runs the whole pipeline for one configured database and records
everything in the AgentStore:

1. Extract metadata/views/procedures through the dialect's adapter.
2. Diff the schema against the last snapshot (git-diff-style change detection).
3. Re-generate AI documentation, reusing the per-database description cache — so
   only changed objects call the LLM — and store the rendered HTML.
4. Scan for PII and compute a 0-100 risk score; detect newly-exposed findings.
5. Run the health checks (where the dialect supports them) and detect degradation.
6. Record a metrics data-point (for trends) and emit timeline events +
   Slack/email notifications for schema changes, new PII, and health degradation.

The whole pass is wrapped so a failure is recorded as a failed run and never
propagates — the daemon keeps polling.
"""
import contextlib
import io
import os
import tempfile

from sqldoc.adapters import get_adapter
from sqldoc.ai import enrich_tables, enrich_views, enrich_procedures
from sqldoc.renderer import render_html
from sqldoc.snapshot import build_snapshot, diff_snapshots, iter_diff_lines
from sqldoc.pii import scan_tables, summarize as pii_summarize, findings_snapshot, diff_findings
from sqldoc.health import collect_health, summarize as health_summarize
from sqldoc.server import collect_server
from sqldoc.logs import collect_logs
from sqldoc.intel import collect_linked_servers
from sqldoc.backup import collect_backups, stale_databases, BACKUP_DIALECTS
from sqldoc.ha import collect_ha, behind_replicas, HA_DIALECTS


def pii_score(high: int, medium: int, low: int) -> float:
    """A 0-100 severity-weighted exposure score (HIGH dominates)."""
    return min(100.0, round(high * 8 + medium * 3 + low * 1, 1))


def _resolve_model(db_config):
    if db_config.model:
        return db_config.model
    return "llama3.1:8b" if db_config.mode == "local" else "claude-haiku-4-5"


def _render_doc(name, tables, views, procedures) -> str:
    fd, path = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    try:
        # render_html prints a "written to <tempfile>" line; keep it out of the log.
        with contextlib.redirect_stdout(io.StringIO()):
            render_html(name, tables, path, views=views, procedures=procedures)
        with open(path, encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _schema_summary(diff) -> str:
    lines = [text for kind, text in iter_diff_lines(diff) if kind != "none"]
    return "\n".join(lines)


def poll_database(store, db_config, agent_config, notifier) -> dict:
    """Run one poll for `db_config`. Returns a small result dict (for logs);
    never raises."""
    name = db_config.name
    run_id = store.start_run(name)
    result = {"db": name, "status": "ok", "schema_changed": False,
              "new_pii": False, "health_degraded": False, "notifications": []}
    try:
        adapter = get_adapter(db_config.connection_string, db_config.dialect)
        tables = adapter.extract_metadata()
        views = adapter.extract_views()
        procedures = adapter.extract_procedures()

        # --- schema diff ---
        new_snap = build_snapshot(name, tables, views, procedures)
        old_snap = store.get_snapshot(name)
        first_run = old_snap is None
        diff = diff_snapshots(old_snap, new_snap) if old_snap else None
        schema_changed = bool(diff and diff["has_changes"])

        # --- AI docs (cache reuse => only changed objects call the LLM) ---
        if not db_config.no_ai:
            model = _resolve_model(db_config)
            cache = store.get_cache(name)
            try:
                enrich_tables(tables, mode=db_config.mode, model=model,
                              concurrency=agent_config.concurrency, cache=cache)
                enrich_views(views, mode=db_config.mode, model=model,
                             concurrency=agent_config.concurrency, cache=cache)
                enrich_procedures(procedures, mode=db_config.mode, model=model,
                                  concurrency=agent_config.concurrency, cache=cache)
                store.save_cache(name, cache)
            except Exception as e:
                store.add_event(name, "error", f"AI enrichment skipped: {type(e).__name__}: {e}")

        store.save_doc(name, _render_doc(name, tables, views, procedures))

        # --- PII ---
        findings = scan_tables(tables)
        psum = pii_summarize(findings)
        risk = psum["by_risk"]
        high, medium, low = risk.get("HIGH", 0), risk.get("MEDIUM", 0), risk.get("LOW", 0)
        score = pii_score(high, medium, low)
        old_pii = store.get_pii_snapshot(name)
        new_pii_snap = findings_snapshot(name, findings)
        pii_diff = diff_findings(old_pii, new_pii_snap) if old_pii else None
        new_pii = bool(pii_diff and (pii_diff["counts"]["added"] or pii_diff["counts"]["changed"]))
        store.save_pii_snapshot(name, new_pii_snap)

        # --- health ---
        health_issues = health_degraded = 0
        health_worse = False
        if adapter.capabilities.health:
            try:
                report = collect_health(adapter)
                hs = health_summarize(report)
                health_issues, health_degraded = hs["issues"], hs["degraded"]
                prev = store.latest_metric(name)
                if prev and health_issues > (prev["health_issues"] or 0):
                    health_worse = True
            except Exception as e:
                store.add_event(name, "error", f"Health check skipped: {type(e).__name__}: {e}")

        store.add_metric(name, tables=len(tables),
                         columns=sum(len(t.columns) for t in tables),
                         pii_high=high, pii_medium=medium, pii_low=low, pii_score=score,
                         health_issues=health_issues, health_degraded=health_degraded)
        store.save_snapshot(name, new_snap)

        # --- events + notifications ---
        if schema_changed:
            result["schema_changed"] = True
            summary = _schema_summary(diff)
            store.add_event(name, "schema_change", _headline(diff), diff)
            result["notifications"] += notifier.notify(
                "schema_change", f"{name}: schema changed", summary)
        if new_pii and not first_run:
            result["new_pii"] = True
            detail = {"added": pii_diff["added"], "risk_changed": pii_diff["risk_changed"]}
            headline = (f"{pii_diff['counts']['added']} new PII finding(s), "
                        f"{pii_diff['counts']['changed']} risk change(s)")
            store.add_event(name, "new_pii", headline, detail)
            result["notifications"] += notifier.notify(
                "new_pii", f"{name}: new PII exposure", headline)
        if health_worse:
            result["health_degraded"] = True
            headline = f"health issues rose to {health_issues}"
            store.add_event(name, "health_degradation", headline)
            result["notifications"] += notifier.notify(
                "health_degradation", f"{name}: health degraded", headline)

        # --- server-level infrastructure monitoring (SQL Server only) ---
        if getattr(agent_config, "server_monitoring", False) and adapter.capabilities.server_monitoring:
            _poll_server_monitoring(store, name, adapter, agent_config, notifier, result)

        # --- backup monitoring (all dialects with a backup/PITR mechanism) ---
        if getattr(agent_config, "backup_monitoring", False) and getattr(adapter, "dialect", "") in BACKUP_DIALECTS:
            _poll_backups(store, name, adapter, agent_config, notifier, result)

        # --- HA / replication monitoring ---
        if getattr(agent_config, "ha_monitoring", False) and getattr(adapter, "dialect", "") in HA_DIALECTS:
            _poll_ha(store, name, adapter, agent_config, notifier, result)

        store.finish_run(run_id, "ok")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        store.finish_run(run_id, "error", msg)
        result["status"] = "error"
        result["error"] = msg
    return result


def _poll_server_monitoring(store, name, adapter, agent_config, notifier, result):
    """Poll instance-level metrics and raise the new infrastructure alerts:
    SQL Agent job failures, low disk space, critical ERRORLOG severity, and
    unreachable linked servers. Each probe is isolated so one failing check
    (or missing permission) never aborts the poll."""
    notes = []

    # SQL Agent job failures + low disk space (one server DMV pass).
    try:
        sreport = collect_server(adapter, include_jobs=True)
        failed = [j for j in sreport.agent_jobs if j.failed_last_24h]
        if failed:
            names = ", ".join(j.name for j in failed[:10])
            headline = f"{len(failed)} SQL Agent job(s) failed in the last 24h: {names}"
            store.add_event(name, "job_failure", headline)
            notes += notifier.notify("job_failure", f"{name}: SQL Agent job failure", headline)
            result["job_failures"] = len(failed)

        threshold = agent_config.disk_threshold_percent
        low = [v for v in sreport.volumes if v.total_gb > 0 and v.free_percent < threshold]
        if low:
            desc = "; ".join(f"{v.volume} {v.free_percent}% free" for v in low)
            headline = f"{len(low)} volume(s) below {threshold}% free: {desc}"
            store.add_event(name, "disk_low", headline)
            notes += notifier.notify("disk_low", f"{name}: low disk space", headline)
            result["disk_low"] = len(low)
    except Exception as e:
        store.add_event(name, "error", f"Server metrics skipped: {type(e).__name__}: {e}")

    # ERRORLOG entries at/above the configured severity in the last 24h.
    try:
        sev = agent_config.errorlog_severity
        lreport = collect_logs(adapter, severity=sev, last_hours=24)
        if lreport.entries:
            crit = sum(1 for e in lreport.entries if e.critical)
            headline = (f"{len(lreport.entries)} ERRORLOG entr{'y' if len(lreport.entries)==1 else 'ies'} "
                        f"at severity >= {sev}" + (f" ({crit} critical)" if crit else ""))
            store.add_event(name, "errorlog_critical", headline)
            notes += notifier.notify("errorlog_critical", f"{name}: critical error-log events", headline)
            result["errorlog_critical"] = len(lreport.entries)
    except Exception as e:
        store.add_event(name, "error", f"ERRORLOG read skipped: {type(e).__name__}: {e}")

    # Linked-server connectivity failures.
    try:
        lsr = collect_linked_servers(adapter)
        down = [s for s in lsr.linked_servers if s.reachable is False]
        if down:
            names = ", ".join(s.name for s in down)
            headline = f"{len(down)} linked server(s) unreachable: {names}"
            store.add_event(name, "linked_server_down", headline)
            notes += notifier.notify("linked_server_down", f"{name}: linked server unreachable", headline)
            result["linked_down"] = len(down)
    except Exception as e:
        store.add_event(name, "error", f"Linked-server check skipped: {type(e).__name__}: {e}")

    result["notifications"] += notes


def _poll_backups(store, name, adapter, agent_config, notifier, result):
    """Alert when a database's last backup is older than the configured threshold,
    when a database has never been backed up, or when point-in-time recovery is
    disabled. Dialect-aware; isolated so a failure is recorded, not fatal."""
    try:
        report = collect_backups(adapter)
    except Exception as e:
        store.add_event(name, "error", f"Backup check skipped: {type(e).__name__}: {e}")
        return
    if not report.supported:
        return
    stale = stale_databases(report, agent_config.backup_max_age_hours)
    problems = []
    if not report.pitr_enabled:
        problems.append(f"point-in-time recovery is OFF ({report.pitr_mechanism})")
    if stale:
        names = ", ".join(d.database for d in stale[:10])
        problems.append(f"{len(stale)} database(s) stale/never backed up: {names}")
    if problems:
        headline = "; ".join(problems)
        store.add_event(name, "backup_stale", headline)
        result["notifications"] += notifier.notify(
            "backup_stale", f"{name}: backup/PITR issue", headline)
        result["backup_stale"] = len(stale)


def _poll_ha(store, name, adapter, agent_config, notifier, result):
    """Alert when a replica is unhealthy or lagging beyond the threshold.
    No-op when HA isn't configured on the instance."""
    try:
        report = collect_ha(adapter)
    except Exception as e:
        store.add_event(name, "error", f"HA check skipped: {type(e).__name__}: {e}")
        return
    if not report.ha_enabled:
        return
    behind = behind_replicas(report, agent_config.replica_lag_threshold_seconds)
    if behind:
        parts = []
        for r in behind:
            lag = f"{r.lag_seconds}s behind" if r.lag_seconds is not None else "unhealthy"
            parts.append(f"{r.server or r.role} ({lag})")
        headline = f"{len(behind)} replica(s) lagging/unhealthy: " + "; ".join(parts[:10])
        store.add_event(name, "replica_lag", headline)
        result["notifications"] += notifier.notify(
            "replica_lag", f"{name}: replica lag/health", headline)
        result["replica_lag"] = len(behind)


def _headline(diff) -> str:
    c = diff["counts"]
    parts = []
    if c["added"]:
        parts.append(f"{c['added']} table(s) added")
    if c["removed"]:
        parts.append(f"{c['removed']} table(s) removed")
    if c["modified"]:
        parts.append(f"{c['modified']} table(s) modified")
    vp = (len(diff["views_added"]) + len(diff["views_removed"])
          + len(diff["procedures_added"]) + len(diff["procedures_removed"]))
    if vp:
        parts.append(f"{vp} view/proc change(s)")
    return ", ".join(parts) or "schema changed"
