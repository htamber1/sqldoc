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


def run_daemon(agent_config, store, notifier, stop_event, log=print,
               host="127.0.0.1", poll_fn=None, authn=None) -> int:
    """Start the dashboard + pollers and block until `stop_event`. Returns the
    dashboard port actually bound (useful when port 0 is requested in tests)."""
    poll_fn = poll_fn or poll_database
    server = make_server(store, agent_config.dashboard_port, host, authn=authn)
    bound_port = server.server_address[1]

    dash_thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    dash_thread.start()
    log(f"agent started: monitoring {len(agent_config.databases)} database(s) every "
        f"{agent_config.interval_minutes}m; dashboard http://{host}:{bound_port}")

    poll_threads = []
    for db in agent_config.databases:
        t = threading.Thread(target=poller_loop,
                             args=(store, db, agent_config, notifier, stop_event, log, poll_fn),
                             name=f"poll-{db.name}", daemon=True)
        t.start()
        poll_threads.append(t)

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
    for t in poll_threads:
        t.join(timeout=15)
    if weekly_thread is not None:
        weekly_thread.join(timeout=5)
    if push_thread is not None:
        push_thread.join(timeout=5)
    if escalation_thread is not None:
        escalation_thread.join(timeout=5)
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
