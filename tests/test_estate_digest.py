"""Daily estate change digest: change extraction, aggregation, scheduling, daemon."""
import threading
from datetime import datetime

import pytest

from sqldoc.agent import estate_digest as ed
from sqldoc.agent import daemon
from sqldoc.agent.config import AgentConfig, NotifyConfig, EstateDigestConfig, parse_agent_config
from sqldoc.agent.store import AgentStore


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "agent.db"))


def _diff(tables_added=(), tables_removed=(), cols_added=(), cols_removed=(), procs_added=()):
    tm = []
    if cols_added or cols_removed:
        tm.append({"name": "dbo.T", "added": list(cols_added), "removed": list(cols_removed)})
    return {"tables_added": list(tables_added), "tables_removed": list(tables_removed),
            "tables_modified": tm, "procedures_added": list(procs_added),
            "procedures_removed": []}


# --- extraction ------------------------------------------------------------

def test_extract_schema_change():
    d = ed._extract_schema_change(_diff(tables_added=["dbo.New"], cols_removed=["Old"],
                                        procs_added=["dbo.p1"]))
    assert d["new_tables"] == ["dbo.New"] and d["dropped_columns"] == 1
    assert d["new_procedures"] == ["dbo.p1"]


def test_extract_from_json_string():
    import json
    d = ed._extract_schema_change(json.dumps(_diff(tables_added=["dbo.X"])))
    assert d["new_tables"] == ["dbo.X"]


# --- collect + totals ------------------------------------------------------

def test_collect_and_totals(store):
    store.add_event("srv1", "schema_change", "1 table added", _diff(tables_added=["dbo.A"]))
    store.add_event("srv1", "new_pii", "2 new HIGH findings")
    store.add_event("srv2", "schema_change", "col dropped", _diff(cols_removed=["c1", "c2"]))
    store.add_event("srv2", "error", "ignored")     # not a change type
    by = ed.collect_estate_changes(store, "2000-01-01T00:00:00+00:00")
    assert set(by) == {"srv1", "srv2"}
    t = ed.estate_totals(by)
    assert t["servers_changed"] == 2 and t["new_tables"] == 1
    assert t["dropped_columns"] == 2 and t["new_pii"] == 1


# --- render ----------------------------------------------------------------

def test_render_html_and_text(store):
    store.add_event("srv1", "schema_change", "changes", _diff(tables_added=["dbo.Orders"]))
    by = ed.collect_estate_changes(store, "2000-01-01T00:00:00+00:00")
    totals = ed.estate_totals(by)
    html = ed.render_estate_digest_html(by, totals, "July 12, 2026")
    assert "estate change digest" in html and "dbo.Orders" in html and "srv1" in html
    text = ed.render_estate_digest_text(by, totals, "July 12, 2026")
    assert "srv1" in text


# --- scheduling ------------------------------------------------------------

def test_is_due():
    cfg = EstateDigestConfig(enabled=True, hour=7)
    now = datetime(2026, 7, 12, 8, 0)
    assert ed.is_due(cfg, now, None) is True
    assert ed.is_due(cfg, now, "2026-07-12") is False           # already sent today
    assert ed.is_due(cfg, datetime(2026, 7, 12, 6, 0), None) is False   # before the hour


def test_maybe_send(store):
    ac = AgentConfig(estate_digest=EstateDigestConfig(enabled=True, hour=7),
                     notify=NotifyConfig(smtp={"smtp_host": "x", "to": ["a@x"]}))
    store.add_event("srv1", "schema_change", "1 table", _diff(tables_added=["dbo.A"]))
    sent = []
    ok = ed.maybe_send_estate_digest(store, ac, now=datetime(2026, 7, 12, 8, 0),
                                     send_fn=lambda smtp, subj, html, text: sent.append(subj))
    assert ok and sent and "Estate change digest" in sent[0]
    # idempotent — second call same day sends nothing
    ok2 = ed.maybe_send_estate_digest(store, ac, now=datetime(2026, 7, 12, 9, 0),
                                      send_fn=lambda *a: sent.append("again"))
    assert ok2 is False


def test_maybe_send_disabled_or_no_smtp(store):
    ac = AgentConfig(estate_digest=EstateDigestConfig(enabled=False))
    assert ed.maybe_send_estate_digest(store, ac, now=datetime(2026, 7, 12, 8, 0)) is False
    ac2 = AgentConfig(estate_digest=EstateDigestConfig(enabled=True), notify=NotifyConfig(smtp=None))
    assert ed.maybe_send_estate_digest(store, ac2, now=datetime(2026, 7, 12, 8, 0)) is False


# --- config ----------------------------------------------------------------

def test_parse_estate_digest():
    cfg = {"agent": {"databases": [{"name": "a", "connection_string": "x"}],
                     "estate_digest": {"enabled": True, "hour": 6}}}
    ac = parse_agent_config(cfg)
    assert ac.estate_digest.enabled and ac.estate_digest.hour == 6


def test_parse_estate_digest_bool():
    cfg = {"agent": {"databases": [{"name": "a", "connection_string": "x"}], "estate_digest": True}}
    assert parse_agent_config(cfg).estate_digest.enabled


# --- daemon loop -----------------------------------------------------------

def test_estate_digest_loop_runs(monkeypatch, store):
    stop = threading.Event()
    calls = []
    monkeypatch.setattr("sqldoc.agent.estate_digest.maybe_send_estate_digest",
                        lambda s, ac, log=None: calls.append(True) or stop.set())
    ac = AgentConfig(estate_digest=EstateDigestConfig(enabled=True))
    daemon.estate_digest_loop(store, ac, stop, lambda *a: None, check_seconds=0.01)
    assert calls


def test_estate_digest_loop_noop_when_disabled():
    daemon.estate_digest_loop(None, AgentConfig(), threading.Event(), lambda *a: None)
