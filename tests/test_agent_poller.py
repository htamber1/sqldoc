"""poll_database: the per-database monitoring pass (adapter + AI mocked)."""
import pytest

from sqldoc.agent import poller
from sqldoc.agent.poller import poll_database, pii_score
from sqldoc.agent.store import AgentStore
from sqldoc.agent.config import AgentConfig, DatabaseConfig, NotifyConfig
from sqldoc.adapters.base import Capabilities
from sqldoc.extractor import Table, Column
from conftest import FakeConnection


class PollAdapter:
    def __init__(self, tables, views=None, procs=None, dialect="sqlserver",
                 health=False, conn=None):
        self._t, self._v, self._p = tables, views or [], procs or []
        self.dialect = dialect
        self.display_name = dialect
        self.capabilities = Capabilities(health=health, quality=True, access_audit=False)
        self._conn = conn

    def extract_metadata(self):
        return self._t

    def extract_views(self):
        return self._v

    def extract_procedures(self):
        return self._p

    def connect(self):
        return self._conn

    def cursor(self, conn):
        return conn.cursor()


class RecNotifier:
    def __init__(self, on=None):
        self.calls = []
        self.on = on or ["schema_change", "new_pii", "health_degradation"]

    def notify(self, event_type, title, text):
        if event_type not in self.on:
            return []
        self.calls.append((event_type, title, text))
        return [("test", True, None)]


def _c(name, dt="nvarchar", pk=False):
    return Column(name, dt, 50, True, pk, False, None, None)


def _tables(cols):
    return [Table("dbo", "People", 10, columns=cols)]


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "agent.db"))


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    # Never call an LLM in tests.
    monkeypatch.setattr(poller, "enrich_tables", lambda t, **k: t)
    monkeypatch.setattr(poller, "enrich_views", lambda v, **k: v)
    monkeypatch.setattr(poller, "enrich_procedures", lambda p, **k: p)


def _cfg(no_ai=True):
    db = DatabaseConfig(name="prod", connection_string="cs", dialect="sqlserver", no_ai=no_ai)
    return db, AgentConfig(databases=[db], notify=NotifyConfig())


def _use(monkeypatch, adapter):
    monkeypatch.setattr(poller, "get_adapter", lambda cs, d=None: adapter)


def test_pii_score_weights():
    assert pii_score(0, 0, 0) == 0.0
    assert pii_score(1, 0, 0) == 8.0
    assert pii_score(100, 100, 100) == 100.0     # capped


def test_first_run_no_change_events(monkeypatch, store):
    _use(monkeypatch, PollAdapter(_tables([_c("Id", "int", pk=True), _c("EmailAddress")])))
    db, ac = _cfg()
    notifier = RecNotifier()
    r = poll_database(store, db, ac, notifier)
    assert r["status"] == "ok" and r["schema_changed"] is False
    assert notifier.calls == []                              # first run: baseline only
    assert store.get_snapshot("prod") is not None
    assert store.get_doc("prod")[0] and "People" in store.get_doc("prod")[0]
    assert store.last_run("prod")["status"] == "ok"
    m = store.latest_metric("prod")
    assert m["tables"] == 1 and m["pii_high"] >= 0


def test_schema_change_detected_and_notified(monkeypatch, store):
    db, ac = _cfg()
    notifier = RecNotifier()
    _use(monkeypatch, PollAdapter(_tables([_c("Id", "int", pk=True)])))
    poll_database(store, db, ac, notifier)                    # baseline
    # second poll: a new (non-PII) column appears
    _use(monkeypatch, PollAdapter(_tables([_c("Id", "int", pk=True), _c("SortOrder", "int")])))
    r = poll_database(store, db, ac, notifier)
    assert r["schema_changed"] is True
    assert any(c[0] == "schema_change" for c in notifier.calls)
    sc = [e for e in store.recent_events("prod") if e["type"] == "schema_change"]
    assert sc and "SortOrder" in sc[0]["detail"]


def test_new_pii_detected_on_second_run(monkeypatch, store):
    db, ac = _cfg()
    notifier = RecNotifier()
    _use(monkeypatch, PollAdapter(_tables([_c("Id", "int", pk=True)])))
    poll_database(store, db, ac, notifier)                    # baseline, no PII
    _use(monkeypatch, PollAdapter(_tables([_c("Id", "int", pk=True), _c("EmailAddress")])))
    r = poll_database(store, db, ac, notifier)
    assert r["new_pii"] is True
    assert any(c[0] == "new_pii" for c in notifier.calls)
    assert store.latest_metric("prod")["pii_medium"] >= 1     # email is MEDIUM


def test_health_degradation_detected(monkeypatch, store, fake_health_rows):
    db, ac = _cfg()
    notifier = RecNotifier()
    # poll 1: healthy (empty DMV rows -> 0 issues)
    _use(monkeypatch, PollAdapter(_tables([_c("Id", "int", pk=True)]),
                                  health=True, conn=FakeConnection({})))
    poll_database(store, db, ac, notifier)
    assert store.latest_metric("prod")["health_issues"] == 0
    # poll 2: DMVs now report problems -> issues rise -> degradation event
    _use(monkeypatch, PollAdapter(_tables([_c("Id", "int", pk=True)]),
                                  health=True, conn=FakeConnection(fake_health_rows)))
    r = poll_database(store, db, ac, notifier)
    assert r["health_degraded"] is True
    assert store.latest_metric("prod")["health_issues"] > 0
    assert any(c[0] == "health_degradation" for c in notifier.calls)


def test_poll_records_error_and_does_not_raise(monkeypatch, store):
    def boom(cs, d=None):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(poller, "get_adapter", boom)
    db, ac = _cfg()
    r = poll_database(store, db, ac, RecNotifier())
    assert r["status"] == "error" and "connection refused" in r["error"]
    assert store.last_run("prod")["status"] == "error"
