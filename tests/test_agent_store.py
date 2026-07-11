"""AgentStore: SQLite state for the monitoring daemon (real temp DB, no mocks)."""
import pytest

from sqldoc.agent.store import AgentStore


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "agent.db"))


def test_snapshot_roundtrip_and_upsert(store):
    assert store.get_snapshot("prod") is None
    store.save_snapshot("prod", {"tables": {"a": 1}})
    assert store.get_snapshot("prod") == {"tables": {"a": 1}}
    store.save_snapshot("prod", {"tables": {"a": 2}})       # upsert
    assert store.get_snapshot("prod")["tables"]["a"] == 2


def test_cache_roundtrip(store):
    assert store.get_cache("prod") == {}
    store.save_cache("prod", {"k": "v"})
    assert store.get_cache("prod") == {"k": "v"}


def test_doc_roundtrip(store):
    assert store.get_doc("prod") == (None, None)
    store.save_doc("prod", "<html>hi</html>")
    html, updated = store.get_doc("prod")
    assert html == "<html>hi</html>" and updated


def test_runs_lifecycle(store):
    rid = store.start_run("prod")
    assert store.last_run("prod")["status"] == "running"
    store.finish_run(rid, "ok")
    lr = store.last_run("prod")
    assert lr["status"] == "ok" and lr["finished_at"]
    rid2 = store.start_run("prod")
    store.finish_run(rid2, "error", "boom")
    lr2 = store.last_run("prod")
    assert lr2["status"] == "error" and lr2["error"] == "boom"


def test_events_timeline(store):
    store.add_event("prod", "schema_change", "2 tables added", {"added": ["x", "y"]})
    store.add_event("prod", "new_pii", "1 new HIGH finding")
    store.add_event("warehouse", "health", "degraded")
    prod = store.recent_events("prod")
    assert [e["type"] for e in prod] == ["new_pii", "schema_change"]   # newest first
    assert prod[1]["detail"] and "x" in prod[1]["detail"]
    assert len(store.recent_events()) == 3                              # all dbs


def test_metrics_history_and_latest(store):
    store.add_metric("prod", tables=10, columns=80, pii_high=1, pii_score=42.0)
    store.add_metric("prod", tables=11, columns=85, pii_high=2, pii_score=55.0)
    hist = store.metrics_history("prod")
    assert [m["tables"] for m in hist] == [10, 11]                      # chronological
    assert store.latest_metric("prod")["pii_score"] == 55.0


def test_list_databases(store):
    store.save_snapshot("prod", {})
    store.start_run("warehouse")
    assert store.list_databases() == ["prod", "warehouse"]


def test_shared_across_threads(tmp_path):
    # each method opens its own connection, so a second "thread" (new store on the
    # same file) sees committed writes immediately.
    path = str(tmp_path / "a.db")
    s1 = AgentStore(path)
    s1.save_snapshot("prod", {"v": 1})
    s2 = AgentStore(path)
    assert s2.get_snapshot("prod") == {"v": 1}
