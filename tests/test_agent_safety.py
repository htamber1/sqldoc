"""Agent safety: the notification allowlist, and latch scoping across pollers.

Two defects, both found by reading the agent before running it for the first
time, and both fixed here.

1. ``agent.notifications.on: []`` ENABLED every event type.
   The parse was ``n.get("on") or list(EVENT_TYPES)``; an empty list is falsy, so
   the obvious way to write "turn notifications off" selected all fifteen. An
   operator who believed the agent was muted had in fact authorised it to page
   every configured channel -- the worst direction for a mistake to fail in.

2. The agent poller reset PROCESS-GLOBAL AI backend state once per cycle.
   ``run_daemon`` runs one poller thread per database concurrently, so with two
   databases one thread's cycle-start reset wiped the down-latch the other
   thread's fan-out was relying on, partially restoring the per-object retry
   storm patch 08 exists to prevent.

Everything here is offline: no database, no network, no agent daemon.
"""
import threading
import time

import pytest

from sqldoc import ai
from sqldoc.agent.config import parse_agent_config, EVENT_TYPES
from sqldoc.agent.notify import Notifier


# ============================================================================
# 1. the `on:` allowlist
# ============================================================================

def _parse(notifications):
    """Parse a minimal agent config, optionally with a notifications block."""
    agent = {"databases": [{"name": "d", "connection_string": "sqlite:///x"}]}
    if notifications is not None:
        agent["notifications"] = notifications
    return parse_agent_config({"agent": agent})


def test_explicit_empty_on_list_means_no_events():
    """THE FIX. `on: []` must disable notifications, not enable all of them."""
    cfg = _parse({"on": []})
    assert cfg.notify.on == [], (
        "on: [] selected %d event type(s); an explicit empty list must mean "
        "'notify on nothing'" % len(cfg.notify.on))


def test_empty_on_list_actually_suppresses_every_event_type():
    """The parse result has to reach the dispatcher, not just look right."""
    cfg = _parse({"on": []})
    notifier = Notifier(cfg.notify)
    for event in EVENT_TYPES:
        assert notifier.should_notify(event) is False, f"{event} still enabled"


def test_absent_on_key_keeps_the_historical_default():
    """Absent means 'not configured' -> the previous all-events behaviour."""
    assert _parse({}).notify.on == list(EVENT_TYPES)


def test_absent_notifications_block_keeps_the_historical_default():
    assert _parse(None).notify.on == list(EVENT_TYPES)


def test_null_on_key_is_treated_as_absent():
    """`on:` with no value parses as None, which is 'unset', not 'empty'."""
    assert _parse({"on": None}).notify.on == list(EVENT_TYPES)


def test_explicit_on_list_is_honoured():
    assert _parse({"on": ["schema_change"]}).notify.on == ["schema_change"]


def test_a_bare_string_on_value_is_accepted():
    assert _parse({"on": "schema_change"}).notify.on == ["schema_change"]


def test_unknown_event_name_is_still_rejected():
    with pytest.raises(ValueError) as exc_info:
        _parse({"on": ["not_an_event"]})
    assert "not_an_event" in str(exc_info.value)


def test_non_list_on_value_is_rejected_with_a_useful_message():
    with pytest.raises(ValueError) as exc_info:
        _parse({"on": 42})
    assert "[]" in str(exc_info.value), "the error should point at the way to disable"


def test_empty_on_beats_configured_channels():
    """Belt and braces: credentials present but the allowlist empty -> silent.

    This is the containment property a test run depends on, so assert it
    directly rather than inferring it from should_notify.
    """
    cfg = _parse({"on": [], "slack_webhook": "https://example.invalid/hook",
                  "email": {"smtp_host": "smtp.example.invalid", "to": ["x@example.invalid"]}})
    notifier = Notifier(cfg.notify)
    # If this dispatched, it would raise trying to reach the host.
    assert notifier.notify("schema_change", "t", "b") == []


# ============================================================================
# 2. backend-state scoping across concurrent poller threads
# ============================================================================

@pytest.fixture(autouse=True)
def _clean_state():
    ai.reset_backend_state()
    yield
    ai.reset_backend_state()


def test_the_poller_does_not_reset_global_backend_state():
    """THE FIX, asserted where the defect lived.

    poll_database() used to call ai.reset_backend_state() at the top of its AI
    block. That is process-global, and run_daemon runs one poller thread per
    database, so it wiped a latch a sibling thread was using. The poller must
    now rely on TTL expiry instead.
    """
    import sqldoc.agent.poller as poller_mod
    src = poller_mod.poll_database.__code__.co_names
    assert "reset_backend_state" not in src, (
        "poll_database still calls reset_backend_state(); with one poller thread "
        "per database that wipes another database's active latch")


def test_a_concurrent_cycle_start_cannot_wipe_an_active_latch():
    """Two poller threads sharing process-global state.

    Thread A latches the backend down mid-fan-out; thread B then begins its own
    cycle. A's latch must survive, or A's remaining objects fall back to
    four-attempts-each against a backend already known to be dead.
    """
    ai.mark_backend_down("ollama", "http://dead:11434", "refused")

    started = threading.Event()

    def second_poller_cycle():
        # What a second database's poll cycle now does: probe, never reset.
        ai.probe_backend(mode="local", backend="anthropic")
        started.set()

    t = threading.Thread(target=second_poller_cycle)
    t.start()
    t.join(timeout=10)
    assert started.is_set(), "second poller thread did not finish"

    assert ai.backend_down("ollama") is not None, (
        "a second poller thread's cycle start destroyed the first thread's latch")


def test_latch_is_shared_across_threads_on_purpose():
    """The latch must NOT be thread-local.

    enrich_*() fans out over a ThreadPoolExecutor, so objects are processed on
    worker threads. The latch only bounds anything if one worker's hard failure
    is visible to its siblings. Thread-local state would give each worker an
    empty latch and restore the original per-object storm.
    """
    ai.mark_backend_down("ollama", "http://dead:11434", "refused")
    seen = {}

    def worker():
        seen["down"] = ai.backend_down("ollama")

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)
    assert seen["down"] is not None, "a sibling worker thread cannot see the latch"


def test_latch_expires_so_a_recovered_backend_is_retried(monkeypatch):
    """Recovery without a destructive reset: the entry ages out."""
    monkeypatch.setattr(ai, "BACKEND_STATE_TTL", 0.05)
    ai.mark_backend_down("ollama", "http://dead:11434", "refused")
    assert ai.backend_down("ollama") is not None
    time.sleep(0.12)
    assert ai.backend_down("ollama") is None, "latch never expires; a recovered backend stays refused"


def test_probe_result_expires_too(monkeypatch):
    """Otherwise the memoized 'unreachable' would outlive the latch."""
    calls = {"n": 0}

    def fake_probe_target(*a, **k):
        calls["n"] += 1
        raise OSError("refused")

    monkeypatch.setattr(ai, "BACKEND_STATE_TTL", 0.05)
    monkeypatch.setattr(ai.requests, "get", fake_probe_target)
    ai.probe_backend(mode="local")
    ai.probe_backend(mode="local")
    assert calls["n"] == 1, "probe not memoized within the TTL"
    time.sleep(0.12)
    ai.probe_backend(mode="local")
    assert calls["n"] == 2, "probe result never expires, so recovery is never noticed"


def test_degraded_reporting_respects_expiry(monkeypatch):
    monkeypatch.setattr(ai, "BACKEND_STATE_TTL", 0.05)
    ai.mark_backend_down("ollama", "http://dead:11434", "refused")
    assert ai.degraded() is True
    assert ai.degraded_detail()[0] == "ollama"
    time.sleep(0.12)
    assert ai.degraded() is False
    assert ai.degraded_detail() is None


def test_ttl_is_longer_than_a_degraded_fan_out():
    """A latch that expired mid-run would let the storm back in for one wave.

    A fully degraded 10,000-object fan-out measured ~4.3s, so the TTL needs a
    comfortable margin over that.
    """
    assert ai.BACKEND_STATE_TTL >= 30


def test_reset_still_available_for_tests():
    """Kept deliberately -- it is correct at process scope, wrong per cycle."""
    ai.mark_backend_down("ollama", "http://dead:11434", "refused")
    ai.reset_backend_state()
    assert ai.backend_down("ollama") is None
    assert ai.degraded() is False


def test_a_real_poll_cycle_does_not_wipe_another_databases_latch(tmp_path, monkeypatch):
    """The cross-thread wipe, reproduced end-to-end through poll_database().

    Setup mirrors the daemon: process-global backend state, one poller per
    database. Thread A (database "alpha") has latched the backend down and is
    notionally mid-fan-out. We then run a REAL poll cycle for database "beta".

    Before the fix, beta's cycle opened with ai.reset_backend_state(), which
    cleared alpha's latch, and alpha's remaining objects went back to four
    attempts each against a backend already proven dead.
    """
    from types import SimpleNamespace
    import sqldoc.agent.poller as poller_mod
    from sqldoc.agent.store import AgentStore
    from sqldoc.extractor import Table

    # --- database "alpha" has latched the backend down, mid-fan-out ---
    ai.mark_backend_down("ollama", "http://dead:11434", "refused")
    assert ai.backend_down("ollama") is not None

    # --- a fake adapter so the poll never touches a database or a network ---
    fake_adapter = SimpleNamespace(
        dialect="sqlserver",
        capabilities=SimpleNamespace(health=False, server_monitoring=False),
        extract_metadata=lambda: [Table(schema="dbo", name="T", row_count=0, columns=[],
                                        indexes=[], triggers=[], check_constraints=[],
                                        unique_constraints=[])],
        extract_views=lambda: [],
        extract_procedures=lambda: [],
    )
    monkeypatch.setattr(poller_mod, "get_adapter", lambda cs, dialect: fake_adapter)
    # The probe must not make a real request either.
    monkeypatch.setattr(ai, "probe_backend",
                        lambda *a, **k: (False, "http://dead:11434", "refused"))

    store = AgentStore(str(tmp_path / "agent.db"))
    cfg = parse_agent_config({"agent": {
        "no_ai": False,                       # the AI block must run
        "databases": [{"name": "beta", "connection_string": "sqlite:///x"}],
        "notifications": {"on": []},          # send nothing
    }})
    db_config = cfg.databases[0]

    captured = []

    class StubNotifier:
        def notify(self, event_type, title, text):
            captured.append((event_type, title, text))
            return []

    done = threading.Event()

    def beta_poll_thread():
        poller_mod.poll_database(store, db_config, cfg, StubNotifier())
        done.set()

    t = threading.Thread(target=beta_poll_thread, name="poll-beta")
    t.start()
    t.join(timeout=60)
    assert done.is_set(), "beta's poll cycle did not finish"

    assert ai.backend_down("ollama") is not None, (
        "database beta's poll cycle destroyed database alpha's active down-latch; "
        "alpha's in-flight fan-out would fall back to 4 attempts per object")


# ============================================================================
# 3. the `on:` key through REAL YAML, not a hand-built dict
# ============================================================================
# These exist because the dict-based tests above passed while the feature was
# completely broken in practice. YAML 1.1 -- which PyYAML implements -- resolves
# the BARE key `on` to the boolean True, so `n.get("on")` never matched and every
# YAML config silently fell through to "all fifteen event types". The allowlist
# had never worked from a config file, only from a dict built in a test.
#
# Lesson worth keeping: a config test that skips the parser is not testing the
# config. Always come through the same door the user does.

import yaml


def _on_from_yaml(notifications_block: str):
    doc = """
agent:
  databases:
    - name: d
      connection_string: sqlite:///x
""" + notifications_block
    return parse_agent_config(yaml.safe_load(doc)).notify.on


def test_yaml_bare_on_key_is_a_boolean_in_yaml_1_1():
    """Documents the trap this guards against, so the fix is not 'simplified' away."""
    parsed = yaml.safe_load("notifications:\n  on: [schema_change]\n")
    assert True in parsed["notifications"], (
        "PyYAML no longer coerces a bare `on` key to True; re-check the alias in "
        "parse_agent_config, it may now be unnecessary")
    assert "on" not in parsed["notifications"]


def test_empty_on_list_disables_notifications_from_yaml():
    """THE REAL-WORLD CASE. Before the fix this returned all 15 event types."""
    assert _on_from_yaml("  notifications:\n    on: []") == []


def test_explicit_on_list_is_honoured_from_yaml():
    """The documented syntax in config.py's own docstring example."""
    assert _on_from_yaml(
        "  notifications:\n    on: [schema_change, new_pii]"
    ) == ["schema_change", "new_pii"]


def test_quoted_on_key_also_works_from_yaml():
    """Quoting is the other way a user avoids the YAML 1.1 coercion."""
    assert _on_from_yaml('  notifications:\n    "on": []') == []


def test_absent_on_key_from_yaml_keeps_the_default():
    assert _on_from_yaml("  notifications:\n    slack_webhook: x") == list(EVENT_TYPES)


def test_no_notifications_block_in_yaml_keeps_the_default():
    assert _on_from_yaml("") == list(EVENT_TYPES)


def test_yaml_on_with_channels_configured_still_disables():
    """The containment property, end to end through YAML."""
    on = _on_from_yaml(
        "  notifications:\n"
        "    on: []\n"
        "    slack_webhook: https://example.invalid/hook\n")
    assert on == []
