"""Cover the agent daemon loop functions + _log_result + dbutil.cell."""
import threading

from sqldoc.agent import daemon
from sqldoc.agent.config import AgentConfig, DatabaseConfig, NotifyConfig, WeeklyReportConfig


def test_log_result_variants():
    logs = []
    log = logs.append
    daemon._log_result(log, {"db": "d", "status": "error", "error": "boom"})
    daemon._log_result(log, {"db": "d", "status": "ok", "schema_changed": True,
                             "new_pii": True, "health_degraded": True,
                             "notifications": [("slack", True, None)]})
    daemon._log_result(log, {"db": "d", "status": "ok", "notifications": []})
    assert "poll FAILED" in logs[0]
    assert "schema-change" in logs[1] and "notification" in logs[1]
    assert logs[2].endswith("poll ok")


def test_interval_seconds():
    assert daemon._interval_seconds(AgentConfig(interval_minutes=2)) == 120.0


def test_poller_loop_runs_once_then_stops():
    stop = threading.Event()
    calls = []

    def poll_fn(store, db, ac, notifier):
        calls.append(db.name)
        stop.set()                       # stop after the first poll
        return {"db": db.name, "status": "ok"}
    ac = AgentConfig(interval_minutes=1)
    db = DatabaseConfig(name="prod", connection_string="x")
    daemon.poller_loop(None, db, ac, None, stop, lambda m: None, poll_fn)
    assert calls == ["prod"]


def test_poller_loop_survives_crash():
    stop = threading.Event()
    logs = []

    def poll_fn(*a):
        stop.set()
        raise RuntimeError("kaboom")
    db = DatabaseConfig(name="prod", connection_string="x")
    daemon.poller_loop(None, db, AgentConfig(), None, stop, logs.append, poll_fn)
    assert any("crashed" in m for m in logs)


def test_weekly_report_loop_disabled_returns():
    ac = AgentConfig(weekly_report=WeeklyReportConfig(enabled=False))
    daemon.weekly_report_loop(None, ac, threading.Event(), lambda m: None)  # returns immediately


def test_weekly_report_loop_runs(monkeypatch):
    stop = threading.Event()
    called = []

    def fake_maybe(store, ac, log=None):
        called.append(True)
        stop.set()
    monkeypatch.setattr(daemon, "maybe_send_weekly_report", fake_maybe)
    ac = AgentConfig(weekly_report=WeeklyReportConfig(enabled=True))
    daemon.weekly_report_loop(None, ac, stop, lambda m: None, check_seconds=0.01)
    assert called


def test_integration_push_loop_noop_without_integrations():
    daemon.integration_push_loop(None, AgentConfig(), threading.Event(), lambda m: None)


def test_integration_push_loop_runs(monkeypatch):
    stop = threading.Event()
    called = []
    monkeypatch.setattr(daemon, "maybe_push_integrations",
                        lambda ac, store, log=None, notifier=None: called.append(True) or stop.set())
    ac = AgentConfig(integrations=["sharepoint"])
    daemon.integration_push_loop(None, ac, stop, lambda m: None, notifier=None, check_seconds=0.01)
    assert called


def test_escalation_loop_no_manager():
    # A plain object without run_escalations -> returns immediately.
    daemon.escalation_loop(None, object(), threading.Event(), lambda m: None)


def test_escalation_loop_runs(monkeypatch):
    stop = threading.Event()

    class Mgr:
        a = type("A", (), {"escalation_after_minutes": 5})()

        def run_escalations(self, log=None):
            stop.set()
            return 1
    daemon.escalation_loop(None, Mgr(), stop, lambda m: None, check_seconds=0.01)


def test_watch_stop_flag(tmp_path):
    stop = threading.Event()
    flag = tmp_path / "stop.flag"
    t = threading.Thread(target=daemon.watch_stop_flag, args=(str(flag), stop, 0.01), daemon=True)
    t.start()
    flag.write_text("stop")
    assert stop.wait(2.0)
    t.join(timeout=1)


# --- dbutil ----------------------------------------------------------------

def test_cell_dict_and_attr():
    from sqldoc.dbutil import cell

    class Row:
        def __init__(self):
            self.name = "attr"
    assert cell({"name": "dict"}, "name") == "dict"
    assert cell(Row(), "name") == "attr"
