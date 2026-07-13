"""The agent daemon: a dashboard thread + one poller thread per database.

`run_daemon` wires everything together and blocks until `stop_event` is set,
then shuts the dashboard and poller threads down gracefully. Each poller loops
`poll_database` on the configured interval, waking early when asked to stop
(`stop_event.wait(interval)` returns immediately once set), so shutdown is fast.
The whole thing is plain Python threading — no external service manager.
"""
import threading
import time

from sqldoc.agent.dashboard import make_server
from sqldoc.agent.poller import poll_database
from sqldoc.agent.weekly import maybe_send_weekly_report
from sqldoc.agent.integrations_push import maybe_push as maybe_push_integrations


_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                  "Saturday", "Sunday"]


def _interval_seconds(agent_config) -> float:
    return max(1, int(agent_config.interval_minutes)) * 60.0


def _log_result(log, r):
    if r.get("status") != "ok":
        log(f"[{r['db']}] poll FAILED: {r.get('error')}")
        return
    tags = []
    if r.get("schema_changed"):
        tags.append("schema-change")
    if r.get("new_pii"):
        tags.append("new-pii")
    if r.get("health_degraded"):
        tags.append("health-degraded")
    n = len(r.get("notifications", []))
    suffix = (" [" + ", ".join(tags) + "]") if tags else ""
    note = f" ({n} notification(s) sent)" if n else ""
    log(f"[{r['db']}] poll ok{suffix}{note}")


def poller_loop(store, db_config, agent_config, notifier, stop_event, log, poll_fn):
    """Poll one database immediately, then every interval, until stop."""
    interval = _interval_seconds(agent_config)
    while not stop_event.is_set():
        try:
            _log_result(log, poll_fn(store, db_config, agent_config, notifier))
        except Exception as e:   # poll_fn shouldn't raise, but never kill the thread
            log(f"[{db_config.name}] poll crashed: {type(e).__name__}: {e}")
        if stop_event.wait(interval):
            break


def weekly_report_loop(store, agent_config, stop_event, log, check_seconds=900):
    """Every check_seconds, send the weekly digest if it's due (idempotent per
    calendar week). Runs only when weekly_report is enabled."""
    wr = getattr(agent_config, "weekly_report", None)
    if not wr or not wr.enabled:
        return
    while not stop_event.is_set():
        try:
            maybe_send_weekly_report(store, agent_config, log=log)
        except Exception as e:
            log(f"weekly scheduler crashed: {type(e).__name__}: {e}")
        if stop_event.wait(check_seconds):
            break


def integration_push_loop(store, agent_config, stop_event, log, notifier=None,
                          check_seconds=1800):
    """Every check_seconds, auto-push docs to any integration whose push interval
    has elapsed. Runs only when agent.integrations is non-empty."""
    if not getattr(agent_config, "integrations", None):
        return
    while not stop_event.is_set():
        try:
            maybe_push_integrations(agent_config, store, log=log, notifier=notifier)
        except Exception as e:
            log(f"integration push scheduler crashed: {type(e).__name__}: {e}")
        if stop_event.wait(check_seconds):
            break


def escalation_loop(store, notifier, stop_event, log, check_seconds=60):
    """Every check_seconds, escalate any open critical/high alert past its
    escalate_at. Runs only when the notifier is an AlertManager with escalation
    configured."""
    run = getattr(notifier, "run_escalations", None)
    a = getattr(notifier, "a", None)
    if run is None or a is None or a.escalation_after_minutes <= 0:
        return
    while not stop_event.is_set():
        try:
            run(log=log)
        except Exception as e:
            log(f"escalation scheduler crashed: {type(e).__name__}: {e}")
        if stop_event.wait(check_seconds):
            break


def cms_reconcile_loop(store, agent_config, notifier, stop_event, log, start_fn, stop_fn,
                       monitored_provider, check_seconds=900):
    """Every check_seconds, reconcile the CMS registration: start monitoring newly
    added servers, stop removed ones, and alert on unreachable servers."""
    if not getattr(agent_config, "cms", None):
        return
    from sqldoc.agent.cms_monitor import reconcile_once
    while not stop_event.is_set():
        try:
            reconcile_once(store, agent_config.cms, notifier, monitored_provider(),
                           start_fn, stop_fn, log=log)
        except Exception as e:
            log(f"cms reconcile crashed: {type(e).__name__}: {e}")
        if stop_event.wait(check_seconds):
            break


def _expand_cms_databases(agent_config, log):
    """When agent.cms is set, add a DatabaseConfig per registered server at startup."""
    if not getattr(agent_config, "cms", None):
        return
    try:
        from sqldoc.agent.cms_monitor import discover, build_databases
        inv = discover(agent_config.cms)
        existing = {d.name for d in agent_config.databases}
        for db in build_databases(inv, agent_config.cms):
            if db.name not in existing:
                agent_config.databases.append(db)
        log(f"cms: monitoring {len(inv.servers)} registered server(s) from "
            f"{agent_config.cms.get('server')}")
    except Exception as e:
        log(f"cms: initial discovery failed ({type(e).__name__}: {e}); "
            f"monitoring explicit databases only")


def estate_digest_loop(store, agent_config, stop_event, log, check_seconds=900):
    """Every check_seconds, send the daily estate change digest if due (idempotent
    per calendar day). Runs only when estate_digest is enabled."""
    ed = getattr(agent_config, "estate_digest", None)
    if not ed or not ed.enabled:
        return
    from sqldoc.agent.estate_digest import maybe_send_estate_digest
    while not stop_event.is_set():
        try:
            maybe_send_estate_digest(store, agent_config, log=log)
        except Exception as e:
            log(f"estate digest scheduler crashed: {type(e).__name__}: {e}")
        if stop_event.wait(check_seconds):
            break


def run_daemon(agent_config, store, notifier, stop_event, log=print,
               host="127.0.0.1", poll_fn=None, authn=None) -> int:
    """Start the dashboard + pollers and block until `stop_event`. Returns the
    dashboard port actually bound (useful when port 0 is requested in tests)."""
    poll_fn = poll_fn or poll_database
    _expand_cms_databases(agent_config, log)
    server = make_server(store, agent_config.dashboard_port, host, authn=authn)
    bound_port = server.server_address[1]

    dash_thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    dash_thread.start()
    log(f"agent started: monitoring {len(agent_config.databases)} database(s) every "
        f"{agent_config.interval_minutes}m; dashboard http://{host}:{bound_port}")

    # Poller registry with a per-database stop event, so the CMS reconcile loop can
    # start/stop monitoring individual servers at runtime.
    poll_threads = []
    poll_stops = {}
    poll_lock = threading.Lock()

    def start_poller(db):
        with poll_lock:
            if db.name in poll_stops:
                return
            ev = threading.Event()
            poll_stops[db.name] = ev
        t = threading.Thread(target=poller_loop,
                             args=(store, db, agent_config, notifier, ev, log, poll_fn),
                             name=f"poll-{db.name}", daemon=True)
        t.start()
        poll_threads.append(t)

    def stop_poller(name):
        with poll_lock:
            ev = poll_stops.pop(name, None)
        if ev is not None:
            ev.set()

    def monitored_names():
        with poll_lock:
            return set(poll_stops)

    for db in agent_config.databases:
        start_poller(db)

    cms_thread = None
    if getattr(agent_config, "cms", None):
        cms_thread = threading.Thread(
            target=cms_reconcile_loop,
            args=(store, agent_config, notifier, stop_event, log, start_poller, stop_poller,
                  monitored_names, max(60, int(agent_config.cms_reconcile_minutes * 60))),
            name="cms-reconcile", daemon=True)
        cms_thread.start()
        log(f"cms reconcile scheduled every {agent_config.cms_reconcile_minutes}m")

    digest_thread = None
    ed = getattr(agent_config, "estate_digest", None)
    if ed is not None and getattr(ed, "enabled", False):
        digest_thread = threading.Thread(
            target=estate_digest_loop, args=(store, agent_config, stop_event, log),
            name="estate-digest", daemon=True)
        digest_thread.start()
        log(f"estate change digest scheduled daily at {ed.hour:02d}:00")

    wr = getattr(agent_config, "weekly_report", None)
    weekly_thread = None
    if wr and wr.enabled:
        weekly_thread = threading.Thread(
            target=weekly_report_loop, args=(store, agent_config, stop_event, log),
            name="weekly-report", daemon=True)
        weekly_thread.start()
        log(f"weekly digest scheduled for {_WEEKDAY_NAMES[wr.weekday]} "
            f"{wr.hour:02d}:00 (emailed to the notifications address)")

    push_thread = None
    if getattr(agent_config, "integrations", None):
        push_thread = threading.Thread(
            target=integration_push_loop,
            args=(store, agent_config, stop_event, log, notifier),
            name="integration-push", daemon=True)
        push_thread.start()
        log(f"integration auto-push scheduled every {agent_config.push_interval_hours}h "
            f"to: {', '.join(agent_config.integrations)}")

    escalation_thread = None
    a = getattr(notifier, "a", None)
    if a is not None and getattr(a, "escalation_after_minutes", 0) > 0:
        escalation_thread = threading.Thread(
            target=escalation_loop, args=(store, notifier, stop_event, log),
            name="escalation", daemon=True)
        escalation_thread.start()
        log(f"alert escalation active: unacked {'/'.join(a.escalation_severities)} "
            f"alerts escalate after {a.escalation_after_minutes}m to "
            f"{', '.join(a.escalation_channels) or 'no channels'}")

    stop_event.wait()
    log("stopping agent...")
    server.shutdown()
    # Signal every per-database poller to stop.
    with poll_lock:
        for ev in poll_stops.values():
            ev.set()
    for t in poll_threads:
        t.join(timeout=15)
    if weekly_thread is not None:
        weekly_thread.join(timeout=5)
    if push_thread is not None:
        push_thread.join(timeout=5)
    if escalation_thread is not None:
        escalation_thread.join(timeout=5)
    if cms_thread is not None:
        cms_thread.join(timeout=5)
    if digest_thread is not None:
        digest_thread.join(timeout=5)
    server.server_close()
    log("agent stopped")
    return bound_port


def watch_stop_flag(stop_flag_path, stop_event, poll_seconds=1.0):
    """Background helper: set `stop_event` when the stop-flag file appears."""
    import os
    while not stop_event.is_set():
        if os.path.exists(stop_flag_path):
            stop_event.set()
            return
        time.sleep(poll_seconds)
