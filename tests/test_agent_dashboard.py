"""Dashboard rendering + a live request against the stdlib server, and the
daemon threading lifecycle."""
import threading
import urllib.request

import pytest

from sqldoc.agent import dashboard, daemon
from sqldoc.agent.store import AgentStore
from sqldoc.agent.config import AgentConfig, DatabaseConfig, NotifyConfig
from sqldoc.agent.dashboard import (render_overview, render_db_page, overview_json,
                                    _sparkline, make_server)
from sqldoc.agent.daemon import run_daemon


@pytest.fixture
def store(tmp_path):
    s = AgentStore(str(tmp_path / "agent.db"))
    rid = s.start_run("prod")
    s.finish_run(rid, "ok")
    s.save_snapshot("prod", {"tables": {}})
    s.save_doc("prod", "<html><body>DOC for prod</body></html>")
    s.add_metric("prod", tables=12, columns=90, pii_high=2, pii_medium=3, pii_low=5,
                 pii_score=37.0, health_issues=4)
    s.add_metric("prod", tables=13, columns=95, pii_high=3, pii_score=48.0, health_issues=6)
    s.add_event("prod", "schema_change", "1 table added", {"x": 1})
    s.add_event("prod", "new_pii", "2 new HIGH findings")
    return s


# --- pure rendering --------------------------------------------------------

def test_sparkline_handles_edge_cases():
    assert "polyline" not in _sparkline([])           # empty -> no line
    assert "polyline" not in _sparkline([5])          # single point
    assert "polyline" in _sparkline([1, 5, 3, 8])


def test_render_overview(store):
    h = render_overview(store)
    assert "prod" in h and "PII risk" in h
    assert "48" in h            # latest pii_score
    assert "13" in h            # latest table count


def test_render_overview_empty(tmp_path):
    empty = AgentStore(str(tmp_path / "e.db"))
    assert "No databases monitored yet" in render_overview(empty)


def test_render_alerts_history(store):
    store.add_alert("prod", "disk_low", "critical", "prod: low disk", "5% free",
                    status="fired", channels="pagerduty,teams")
    store.add_alert("prod", "schema_change", "medium", "prod: changed", "x",
                    status="suppressed_dedup")
    h = dashboard.render_alerts(store)
    assert "Alert history" in h
    assert "critical" in h and "disk_low" in h
    assert "pagerduty,teams" in h
    assert "suppressed (duplicate)" in h


def test_render_alerts_empty(tmp_path):
    empty = AgentStore(str(tmp_path / "e.db"))
    assert "No alerts recorded yet" in dashboard.render_alerts(empty)


def test_overview_links_alerts(store):
    assert "/alerts" in render_overview(store)


def test_render_db_page(store):
    h = render_db_page(store, "prod")
    assert "Change timeline" in h and "schema_change" in h and "new_pii" in h
    assert "Trends" in h and "polyline" in h          # sparkline rendered
    assert "/db/prod/doc" in h                         # link to full docs


def test_render_db_page_unknown(store):
    assert "Unknown database" in render_db_page(store, "nope")


def test_overview_json(store):
    data = overview_json(store)
    assert data["databases"][0]["name"] == "prod"
    assert data["databases"][0]["pii_score"] == 48.0


# --- live server -----------------------------------------------------------

def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_live_server_routes(store):
    server = make_server(store, 0)                     # ephemeral port
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        assert _get(port, "/")[0] == 200
        assert "prod" in _get(port, "/")[1]
        assert "DOC for prod" in _get(port, "/db/prod/doc")[1]
        assert "Change timeline" in _get(port, "/db/prod")[1]
        code, body = _get(port, "/api/overview")
        assert code == 200 and '"pii_score": 48.0' in body
    finally:
        server.shutdown()
        server.server_close()


def test_live_server_404(store):
    server = make_server(store, 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/nope")
        assert ei.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


# --- daemon lifecycle ------------------------------------------------------

def test_poller_loop_stops_via_event(store, monkeypatch):
    monkeypatch.setattr(daemon, "_interval_seconds", lambda ac: 0.01)  # fast loop
    calls = []
    stop = threading.Event()

    def poll_fn(st, db, ac, notifier):
        calls.append(db.name)
        if len(calls) >= 3:
            stop.set()
        return {"db": db.name, "status": "ok", "notifications": []}

    db = DatabaseConfig(name="prod", connection_string="cs")
    ac = AgentConfig(interval_minutes=1, databases=[db])
    daemon.poller_loop(store, db, ac, None, stop, log=lambda *_: None, poll_fn=poll_fn)
    assert len(calls) == 3      # looped multiple times, stopped promptly after the 3rd


def test_run_daemon_lifecycle(store):
    stop = threading.Event()
    polled = []

    def poll_fn(st, db, ac, notifier):
        polled.append(db.name)
        return {"db": db.name, "status": "ok", "notifications": []}

    dbs = [DatabaseConfig(name="prod", connection_string="a"),
           DatabaseConfig(name="wh", connection_string="b")]
    ac = AgentConfig(interval_minutes=1, dashboard_port=0, databases=dbs)
    logs = []

    def run():
        run_port.append(run_daemon(ac, store, None, stop, log=logs.append, poll_fn=poll_fn))
    run_port = []
    t = threading.Thread(target=run)
    t.start()
    # give the threads a moment to poll once + bind the dashboard
    import time
    for _ in range(50):
        if "prod" in polled and "wh" in polled:
            break
        time.sleep(0.05)
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert set(polled) >= {"prod", "wh"}
    assert any("agent started" in m for m in logs)
    assert any("agent stopped" in m for m in logs)
