"""Regression tests for server_monitoring's linked-server path (v3.2.0, 15-16).

`server_monitoring` was validated WITHOUT ever letting the outbound probe fire.
In the environment where this was found, dev instances carry linked servers that
can reach production, and `sp_testlinkedserver` makes the SQL Server itself open
a connection to each one -- a network egress guard in the client process cannot
stop that. So the probe was neutralised (synthetic enumeration plus a DB-side
guard that raises on any outbound-shaped statement) and the logic exercised
around it. No linked server was contacted at any point.

PATCH 15 -- `probe_connectivity` collapsed a three-state answer into two.
    `LinkedServer.reachable` is documented three-state ("None = not tested").
    Every consumer honours it: the agent counts `reachable is False` as down,
    `summarize_linked` counts `is False` as unreachable, and the renderer has a
    grey "not tested" pill. But `probe_connectivity` returned False for ANY
    exception -- so a denied EXECUTE permission, or RPC simply being switched
    off, produced a false "linked server unreachable" alert naming servers that
    may be perfectly healthy. One path did not honour the contract every other
    path assumed.

PATCH 16 -- `_poll_server_monitoring` discarded `report.errors`.
    `collect_linked_servers()` degrades by ACCUMULATING failures into
    `report.errors` instead of raising, so the handler's `except` never sees
    them. A denied `sys.servers` read therefore produced a completely silent
    poll: no findings, no error, indistinguishable from a clean estate.

Run with:  pytest test_server_monitoring.py
"""
import inspect
import re

import pytest

from sqldoc import intel
from sqldoc.agent import poller as poller_mod
from sqldoc.agent.config import AgentConfig, parse_agent_config


# --- fakes -----------------------------------------------------------------

class RaisingCursor:
    def __init__(self, exc):
        self.exc = exc
        self.statements = []

    def execute(self, stmt, *a, **kw):
        self.statements.append(str(stmt))
        raise self.exc


class OkCursor:
    def __init__(self):
        self.statements = []

    def execute(self, stmt, *a, **kw):
        self.statements.append(str(stmt))
        return self

    def fetchall(self):
        return []


class FakeStore:
    def __init__(self):
        self.events = []

    def add_event(self, db, type, summary, detail=None):
        self.events.append((type, summary))


class FakeNotifier:
    def notify(self, *a, **kw):
        return []


class Cfg:
    server_monitoring = True
    disk_threshold_percent = 10.0
    errorlog_severity = 17
    tempdb_version_store_mb = 2048.0


def server(name, reachable):
    s = intel.LinkedServer(name=name)
    s.reachable = reachable
    return s


@pytest.fixture
def isolated_handler(monkeypatch):
    """Neutralise the two sibling probes so the linked-server branch is alone."""
    def unavailable(*a, **kw):
        raise RuntimeError("not under test")
    monkeypatch.setattr(poller_mod, "collect_server", unavailable)
    monkeypatch.setattr(poller_mod, "collect_logs", unavailable)
    return None


# The isolation fixture makes the two sibling probes raise, and the handler
# records an error event for each. Those are expected noise; every assertion
# below looks only at the linked-server events.
SIBLING_NOISE = ("Server metrics skipped", "ERRORLOG read skipped")


def linked_events(store):
    return [(t, s) for t, s in store.events
            if not any(n in s for n in SIBLING_NOISE)]


def run_handler(monkeypatch, report):
    store = FakeStore()
    result = {"notifications": []}
    monkeypatch.setattr(poller_mod, "collect_linked_servers", lambda adapter: report)
    poller_mod._poll_server_monitoring(store, "db", object(), Cfg(), FakeNotifier(), result)
    return store, result


# --- the flag gates the behaviour -----------------------------------------

def test_server_monitoring_defaults_off():
    assert AgentConfig().server_monitoring is False


DB_ENTRY = {"name": "d", "dialect": "sqlserver",
            "connection_string": "DRIVER={x};SERVER=s;DATABASE=d;"}


def _cfg(**agent):
    agent.setdefault("databases", [DB_ENTRY])
    return parse_agent_config({"agent": agent})


def test_absent_in_config_leaves_it_off():
    assert _cfg().server_monitoring is False


def test_explicit_false_leaves_it_off():
    assert _cfg(server_monitoring=False).server_monitoring is False


def test_only_explicit_true_turns_it_on():
    assert _cfg(server_monitoring=True).server_monitoring is True


def test_handler_is_guarded_by_the_flag_in_poll_database():
    """The linked-server probe must be unreachable unless the flag is set."""
    src = inspect.getsource(poller_mod.poll_database)
    call = [l for l in src.splitlines() if "_poll_server_monitoring(" in l]
    assert call, "call site not found"
    idx = src.splitlines().index(call[0])
    guard = src.splitlines()[idx - 1]
    assert "server_monitoring" in guard, guard
    assert re.search(r'getattr\(\s*agent_config\s*,\s*["\']server_monitoring["\']\s*,\s*False\s*\)',
                     guard), guard


def test_linked_server_probe_only_reachable_through_that_handler():
    """No other poller path may issue the outbound probe."""
    import ast
    tree = ast.parse(inspect.getsource(poller_mod))
    callers = sorted(
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(c, ast.Call) and getattr(c.func, "id", None)
                == "collect_linked_servers" for c in ast.walk(node)))
    assert callers == ["_poll_server_monitoring"], callers


def test_no_linked_server_sql_when_flag_off(monkeypatch):
    """With the flag off the handler never runs, so no statement is issued."""
    issued = []

    def spy(adapter):
        issued.append("collect_linked_servers")
        raise AssertionError("must not run when server_monitoring is off")

    monkeypatch.setattr(poller_mod, "collect_linked_servers", spy)
    cfg = _cfg()
    assert cfg.server_monitoring is False
    # the gate in poll_database is `if getattr(cfg,'server_monitoring',False) and ...`
    if getattr(cfg, "server_monitoring", False):
        poller_mod._poll_server_monitoring(FakeStore(), "db", object(), cfg,
                                           FakeNotifier(), {"notifications": []})
    assert issued == [], "linked-server collection ran with the flag off"


# --- patch 15: three-state probe result ------------------------------------

@pytest.mark.parametrize("message", [
    "The EXECUTE permission was denied on the object 'sp_testlinkedserver'",
    "Server 'LS_ONE' is not configured for RPC.",
    "The server 'LS_ONE' is not configured for DATA ACCESS.",
])
def test_probe_that_could_not_run_is_not_tested_not_down(message):
    reachable, msg = intel.probe_connectivity(RaisingCursor(PermissionError(message)), "LS_ONE")
    assert reachable is None, f"classified as {reachable!r}: {msg}"
    assert "not tested" in msg


def test_genuine_connectivity_failure_is_still_down():
    exc = OSError("TCP Provider: No such host is known.")
    reachable, msg = intel.probe_connectivity(RaisingCursor(exc), "LS_ONE")
    assert reachable is False
    assert "not tested" not in msg


def test_successful_probe_is_reachable():
    reachable, msg = intel.probe_connectivity(OkCursor(), "LS_ONE")
    assert reachable is True and msg == "OK"


def test_untested_server_is_not_counted_as_unreachable(monkeypatch, isolated_handler):
    report = intel.LinkedServerReport()
    report.linked_servers = [server("LS_ONE", None), server("LS_TWO", None)]
    store, result = run_handler(monkeypatch, report)
    assert result.get("linked_down") is None, "untested servers raised a down alert"
    assert not [s for t, s in linked_events(store) if t == "linked_server_down"]


def test_untested_servers_are_still_surfaced(monkeypatch, isolated_handler):
    """Not-tested must not be silent either -- just not reported as down."""
    report = intel.LinkedServerReport()
    report.linked_servers = [server("LS_ONE", None)]
    store, _ = run_handler(monkeypatch, report)
    assert any("could not be tested" in s for _, s in linked_events(store))


def test_genuinely_down_server_still_alerts(monkeypatch, isolated_handler):
    report = intel.LinkedServerReport()
    report.linked_servers = [server("LS_ONE", True), server("LS_TWO", False)]
    store, result = run_handler(monkeypatch, report)
    assert result.get("linked_down") == 1
    down = [s for t, s in linked_events(store) if t == "linked_server_down"]
    assert down and "LS_TWO" in down[0] and "LS_ONE" not in down[0]


def test_mixed_states_are_separated(monkeypatch, isolated_handler):
    report = intel.LinkedServerReport()
    report.linked_servers = [server("UP", True), server("DOWN", False),
                             server("UNKNOWN", None)]
    store, result = run_handler(monkeypatch, report)
    assert result.get("linked_down") == 1
    down = [s for t, s in linked_events(store) if t == "linked_server_down"][0]
    assert "DOWN" in down and "UNKNOWN" not in down
    assert any("could not be tested" in s and "UNKNOWN" in s
               for _, s in linked_events(store))


# --- patch 16: collector errors are surfaced -------------------------------

def test_enumeration_failure_is_recorded(monkeypatch, isolated_handler):
    """collect_linked_servers accumulates instead of raising -- surface it."""
    report = intel.LinkedServerReport()
    report.errors.append(("Discover linked servers",
                          "PermissionError: SELECT permission was denied on 'servers'"))
    store, result = run_handler(monkeypatch, report)
    errs = [s for t, s in linked_events(store) if t == "error"]
    assert errs, "a denied enumeration produced a completely silent poll"
    assert any("Discover linked servers" in e for e in errs)


def test_clean_estate_stays_quiet(monkeypatch, isolated_handler):
    report = intel.LinkedServerReport()
    report.linked_servers = [server("LS_ONE", True), server("LS_TWO", True)]
    store, result = run_handler(monkeypatch, report)
    assert linked_events(store) == []
    assert result.get("linked_down") is None


def test_no_linked_servers_at_all_is_quiet(monkeypatch, isolated_handler):
    store, result = run_handler(monkeypatch, intel.LinkedServerReport())
    assert linked_events(store) == []
