"""Agent auto-push scheduling + config, with the connector clients and DB access
fully mocked."""
import pytest

from sqldoc.agent import integrations_push as ip
from sqldoc.agent.config import parse_agent_config
from sqldoc.agent.store import AgentStore


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "agent.db"))


BASE_CFG = {
    "agent": {
        "databases": [{"name": "prod", "connection_string": "sqlite:///x.db"}],
    },
    "sharepoint": {"tenant_id": "t", "client_id": "c", "client_secret": "s",
                   "site_id": "site"},
}


# --- config parsing --------------------------------------------------------

def test_parse_integrations_and_interval():
    cfg = {**BASE_CFG}
    cfg["agent"] = {**BASE_CFG["agent"], "integrations": ["sharepoint"],
                    "push_interval_hours": 12}
    ac = parse_agent_config(cfg)
    assert ac.integrations == ["sharepoint"]
    assert ac.push_interval_hours == 12
    assert ac.raw_config is cfg


def test_parse_rejects_unknown_integration():
    cfg = {"agent": {**BASE_CFG["agent"], "integrations": ["not_a_thing"]}}
    with pytest.raises(ValueError) as e:
        parse_agent_config(cfg)
    assert "not_a_thing" in str(e.value)


def test_parse_rejects_low_interval():
    cfg = {"agent": {**BASE_CFG["agent"], "integrations": ["sharepoint"],
                     "push_interval_hours": 0}}
    with pytest.raises(ValueError):
        parse_agent_config(cfg)


def test_defaults_no_integrations():
    ac = parse_agent_config(BASE_CFG)
    assert ac.integrations == []
    assert ac.push_interval_hours == 24.0


# --- push_once -------------------------------------------------------------

class _FakeClient:
    def __init__(self, config):
        self.config = config
        self.pushed = []

    def push_reports(self, artifacts, metrics=None):
        self.pushed.append((artifacts, metrics))
        return {"ok": True, "detail": f"pushed {len(artifacts)} report(s)"}


def _patch_collection(monkeypatch, client_holder):
    """Stub adapter/gather/render so no DB or renderer runs."""
    monkeypatch.setattr(ip, "_adapter_for", lambda db: object())
    monkeypatch.setattr(ip, "gather", lambda adapter, name, **k: _Bundle(name))
    monkeypatch.setattr(ip, "render_artifacts", lambda bundle, *a, **k: ["A", "B"])
    monkeypatch.setattr(ip, "bundle_metrics", lambda bundle: {"database": bundle.database})

    def fake_get_client(name, conf):
        c = _FakeClient(conf)
        client_holder.append(c)
        return c
    monkeypatch.setattr(ip, "get_client", fake_get_client)


class _Bundle:
    def __init__(self, database):
        self.database = database
        self.notes = []


def test_push_once_pushes_all(monkeypatch, store):
    holder = []
    _patch_collection(monkeypatch, holder)
    ac = parse_agent_config({"agent": {**BASE_CFG["agent"], "integrations": ["sharepoint"]},
                             "sharepoint": BASE_CFG["sharepoint"]})
    results = ip.push_once(ac, store, log=lambda *a: None)
    assert results and all(r["ok"] for r in results)
    assert holder[0].pushed          # the fake client received a push
    events = store.recent_events("prod")
    assert any(e["type"] == "integration_push" for e in events)


def test_push_once_isolates_failures(monkeypatch, store):
    holder = []
    _patch_collection(monkeypatch, holder)

    def boom(name, conf):
        raise RuntimeError("bad config")
    monkeypatch.setattr(ip, "get_client", boom)
    ac = parse_agent_config({"agent": {**BASE_CFG["agent"], "integrations": ["sharepoint"]},
                             "sharepoint": BASE_CFG["sharepoint"]})
    results = ip.push_once(ac, store, log=lambda *a: None)
    assert results and results[0]["ok"] is False


# --- scheduling ------------------------------------------------------------

def test_maybe_push_respects_interval(monkeypatch, store):
    holder = []
    _patch_collection(monkeypatch, holder)
    ac = parse_agent_config({"agent": {**BASE_CFG["agent"], "integrations": ["sharepoint"],
                                       "push_interval_hours": 24},
                             "sharepoint": BASE_CFG["sharepoint"]})
    # First call at t=0 fires (no prior timestamp).
    r1 = ip.maybe_push(ac, store, log=lambda *a: None, now=0.0)
    assert r1
    # 1 hour later: not due yet.
    r2 = ip.maybe_push(ac, store, log=lambda *a: None, now=3600.0)
    assert r2 == []
    # 25 hours later: due again.
    r3 = ip.maybe_push(ac, store, log=lambda *a: None, now=25 * 3600.0)
    assert r3


def test_maybe_push_noop_without_integrations(store):
    ac = parse_agent_config(BASE_CFG)
    assert ip.maybe_push(ac, store, log=lambda *a: None, now=0.0) == []


class _RecNotifier:
    def __init__(self):
        self.events = []

    def notify(self, event_type, title, text):
        self.events.append((event_type, title, text))
        return []


def test_push_once_sends_doc_updated_notification(monkeypatch, store):
    holder = []
    _patch_collection(monkeypatch, holder)
    notifier = _RecNotifier()
    ac = parse_agent_config({"agent": {**BASE_CFG["agent"], "integrations": ["sharepoint"]},
                             "sharepoint": BASE_CFG["sharepoint"]})
    ip.push_once(ac, store, log=lambda *a: None, notifier=notifier)
    assert notifier.events and notifier.events[0][0] == "doc_updated"
    assert "sharepoint" in notifier.events[0][1]
