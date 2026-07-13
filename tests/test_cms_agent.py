"""CMS agent monitoring: config, database expansion, reconciliation, daemon loop."""
import threading

import pytest

from sqldoc.agent import cms_monitor, daemon
from sqldoc.agent.config import parse_agent_config, AgentConfig
from sqldoc.agent.store import AgentStore
from sqldoc.cms import CmsInventory, CmsGroup, CmsServer


def _inv(names=("p1", "p2")):
    return CmsInventory(
        cms_server="CMS",
        groups=[CmsGroup(id=2, name="Prod", parent_id=1, path="Prod")],
        servers=[CmsServer(name=n, server_name=f"{n}.corp", group_id=2, group_path="Prod")
                 for n in names])


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "agent.db"))


# --- config ----------------------------------------------------------------

def test_parse_agent_cms_without_databases():
    cfg = {"agent": {"cms": {"server": "CMS01", "windows_auth": True},
                     "cms_reconcile_minutes": 10}}
    ac = parse_agent_config(cfg)
    assert ac.cms["server"] == "CMS01" and ac.cms_reconcile_minutes == 10
    assert ac.databases == []          # populated from the CMS at startup


def test_parse_agent_cms_needs_server():
    with pytest.raises(ValueError):
        parse_agent_config({"agent": {"cms": {"windows_auth": True}}})


def test_parse_agent_requires_dbs_or_cms():
    with pytest.raises(ValueError):
        parse_agent_config({"agent": {}})


# --- build_databases -------------------------------------------------------

def test_build_databases():
    dbs = cms_monitor.build_databases(_inv(), {"windows_auth": True, "database": "master"})
    assert [d.name for d in dbs] == ["p1", "p2"]
    assert "SERVER=p1.corp" in dbs[0].connection_string
    assert "Trusted_Connection=yes" in dbs[0].connection_string and dbs[0].no_ai


# --- reconcile -------------------------------------------------------------

def test_reconcile_added_and_removed():
    added, removed = cms_monitor.reconcile({"p1", "gone"}, _inv(("p1", "p2")))
    assert [s.name for s in added] == ["p2"]
    assert removed == ["gone"]


def test_reconcile_once(monkeypatch, store):
    started, stopped = [], []
    notifs = []

    class Notifier:
        def notify(self, et, title, text):
            notifs.append(et)
    monkeypatch.setattr(cms_monitor, "probe", lambda db: db.name != "p2")   # p2 unreachable
    changes = cms_monitor.reconcile_once(
        store, {"server": "CMS", "windows_auth": True}, Notifier(),
        monitored_names={"p1", "gone"},
        start_fn=lambda db: started.append(db.name),
        stop_fn=lambda name: stopped.append(name),
        discover_fn=lambda: _inv(("p1", "p2")))
    assert changes["added"] == ["p2"] and changes["removed"] == ["gone"]
    assert "p2" in changes["unreachable"]
    assert started == ["p2"] and stopped == ["gone"]
    assert "cms_server_added" in notifs and "cms_server_removed" in notifs
    assert "cms_server_unreachable" in notifs
    events = [e["type"] for e in store.recent_events()]
    assert "cms_server_added" in events and "cms_server_unreachable" in events


def test_reconcile_once_discovery_failure_isolated(store):
    def boom():
        raise RuntimeError("cms down")
    changes = cms_monitor.reconcile_once(
        store, {"server": "CMS"}, None, monitored_names=set(),
        start_fn=lambda db: None, stop_fn=lambda n: None,
        discover_fn=boom, log=lambda *a: None)
    assert changes == {"added": [], "removed": [], "unreachable": []}


# --- daemon loop -----------------------------------------------------------

def test_cms_reconcile_loop_runs_once(monkeypatch, store):
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(cms_monitor, "reconcile_once",
                        lambda *a, **k: calls.append(True) or stop.set() or {})
    ac = AgentConfig(cms={"server": "CMS"})
    daemon.cms_reconcile_loop(store, ac, None, stop, lambda *a: None,
                              start_fn=lambda db: None, stop_fn=lambda n: None,
                              monitored_provider=lambda: set(), check_seconds=0.01)
    assert calls


def test_cms_reconcile_loop_noop_without_cms():
    daemon.cms_reconcile_loop(None, AgentConfig(), None, threading.Event(), lambda *a: None,
                              start_fn=lambda db: None, stop_fn=lambda n: None,
                              monitored_provider=lambda: set())


def test_expand_cms_databases(monkeypatch):
    monkeypatch.setattr("sqldoc.agent.cms_monitor.discover", lambda cms: _inv(("a", "b")))
    ac = AgentConfig(cms={"server": "CMS", "windows_auth": True})
    daemon._expand_cms_databases(ac, lambda *a: None)
    assert {d.name for d in ac.databases} == {"a", "b"}


def test_expand_cms_databases_failure_isolated(monkeypatch):
    def boom(cms):
        raise RuntimeError("no cms")
    monkeypatch.setattr("sqldoc.agent.cms_monitor.discover", boom)
    ac = AgentConfig(cms={"server": "CMS"})
    daemon._expand_cms_databases(ac, lambda *a: None)      # logs, doesn't raise
    assert ac.databases == []
