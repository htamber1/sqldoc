"""Regression tests for backup_monitoring / ha_monitoring (v3.2.0, patches 13-14).

Found running the agent with `backup_monitoring` and `ha_monitoring` enabled
against a non-production database -- the first time either flag has ever been
switched on.

PATCH 13 -- `pitr_enabled` counted system databases.
    `_collect_sqlserver` rolled the instance-level "is point-in-time recovery
    on?" flag up from EVERY database, system databases included. SQL Server
    ships `model` with FULL recovery, so `pitr_enabled` is True on essentially
    every instance, and the "point-in-time recovery is OFF" warning is
    suppressed even where no USER database has PITR at all. Observed live: 11
    databases, every user database SIMPLE, zero user databases PITR-capable --
    and `pitr_enabled` still True, solely because of `model`.

    The same function already excluded system databases from the per-database
    "SIMPLE recovery model" issue. One code path used the exclusion, the
    sibling path didn't -- the recurring shape in this codebase.

PATCH 14 -- `_poll_ha` discarded the honest "no HA configured" answer.
    On an instance with HADR enabled but no availability group, `collect_ha()`
    correctly returns `ha_enabled=False` and a note saying so. `_poll_ha` then
    did a bare `return`, storing nothing -- making "ha_monitoring is on and
    found no availability group" byte-for-byte indistinguishable from
    "ha_monitoring was never enabled". The posture is now recorded, and only
    when it CHANGES, so a steady state costs one event rather than one a cycle.

Run with:  pytest test_monitoring_flags.py
"""
import pytest

from sqldoc import backup as backup_mod
from sqldoc.agent import poller as poller_mod


# --- fakes -----------------------------------------------------------------

class Row(dict):
    """Row that satisfies sqldoc.dbutil.cell() via the dict branch."""


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **kw):
        return self

    def fetchall(self):
        return self._rows


def db_row(name, recovery, last_full=None, last_log=None, age=None):
    return Row(database_name=name, recovery_model_desc=recovery,
               last_full=last_full, last_diff=None, last_log=last_log,
               full_age_hours=age)


class FakeStore:
    def __init__(self):
        self.events = []
        self.meta = {}

    def add_event(self, db, type, summary, detail=None):
        self.events.append((db, type, summary))

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value


class FakeNotifier:
    def notify(self, *a, **kw):
        return []


class Cfg:
    replica_lag_threshold_seconds = 30.0
    backup_max_age_hours = 24.0


# --- patch 13: pitr_enabled ignores system databases -----------------------

def test_pitr_off_when_only_system_databases_are_full():
    """The live case: every user database SIMPLE, only `model` FULL."""
    rows = [db_row("master", "SIMPLE"), db_row("model", "FULL"),
            db_row("msdb", "SIMPLE"),
            db_row("app_one", "SIMPLE"), db_row("app_two", "SIMPLE")]
    report = backup_mod.collect_backups_from_cursor("sqlserver", FakeCursor(rows))
    assert report.pitr_enabled is False, (
        "`model` being FULL must not suppress the instance PITR warning")


def test_pitr_on_when_a_user_database_is_full():
    rows = [db_row("model", "FULL"), db_row("app_one", "SIMPLE"),
            db_row("app_two", "FULL", last_full="2026-01-01", last_log="2026-01-01")]
    report = backup_mod.collect_backups_from_cursor("sqlserver", FakeCursor(rows))
    assert report.pitr_enabled is True


def test_pitr_on_for_bulk_logged_user_database():
    rows = [db_row("app_one", "BULK_LOGGED", last_full="2026-01-01")]
    report = backup_mod.collect_backups_from_cursor("sqlserver", FakeCursor(rows))
    assert report.pitr_enabled is True


def test_no_user_databases_raises_no_pitr_alarm():
    """An instance with only system databases has nothing to protect."""
    rows = [db_row("master", "SIMPLE"), db_row("model", "SIMPLE"),
            db_row("msdb", "SIMPLE")]
    report = backup_mod.collect_backups_from_cursor("sqlserver", FakeCursor(rows))
    assert report.pitr_enabled is True


def test_system_database_exclusion_is_consistent_across_both_signals():
    """The per-database SIMPLE issue and the rollup must use the same list."""
    rows = [db_row("master", "SIMPLE"), db_row("model", "SIMPLE"),
            db_row("msdb", "SIMPLE"), db_row("app_one", "SIMPLE")]
    report = backup_mod.collect_backups_from_cursor("sqlserver", FakeCursor(rows))
    # v3.3.0: the SIMPLE finding moved from `issues` (a failure) to
    # `informational` (a posture for the DBA to confirm). The exclusion this
    # test exists to pin -- both signals using one system-DB list -- is
    # unchanged; only the field carrying the finding moved.
    flagged = {d.database for d in report.databases
               if any("SIMPLE recovery model" in i for i in d.informational)}
    assert flagged == {"app_one"}, flagged
    assert all(not d.issues or "SIMPLE" not in "".join(d.issues)
               for d in report.databases), "SIMPLE must not be a failure"
    assert report.pitr_enabled is False


def test_per_database_findings_still_accurate():
    rows = [db_row("app_one", "SIMPLE"),
            db_row("app_two", "FULL", last_full="2026-01-01", last_log=None)]
    report = backup_mod.collect_backups_from_cursor("sqlserver", FakeCursor(rows))
    by = {d.database: d for d in report.databases}
    assert by["app_one"].never_backed_up is True
    assert by["app_two"].never_backed_up is False
    assert any("no log backups" in i for i in by["app_two"].issues)


# --- patch 14: _poll_ha records the posture honestly -----------------------

class HaReportStub:
    def __init__(self, enabled, replicas=(), notes=()):
        self.ha_enabled = enabled
        self.replicas = list(replicas)
        self.notes = list(notes)
        self.mechanism = "Always On availability groups"
        self.supported = True
        self.errors = []


def _run_ha(monkeypatch, report, store, result=None):
    monkeypatch.setattr(poller_mod, "collect_ha", lambda adapter: report)
    result = {"notifications": []} if result is None else result
    poller_mod._poll_ha(store, "db-under-test", object(), Cfg(), FakeNotifier(), result)
    return result


def test_no_availability_group_is_reported_not_swallowed(monkeypatch):
    """The live case: HADR on at the instance, zero availability groups."""
    store = FakeStore()
    note = "No Always On availability groups are configured on this instance."
    result = _run_ha(monkeypatch, HaReportStub(False, notes=[note]), store)
    kinds = [t for _, t, _ in store.events]
    assert "ha_posture" in kinds, "no-AG posture was discarded"
    assert any(note in s for _, _, s in store.events)
    assert result["ha_enabled"] is False


def test_enabled_and_no_ag_are_distinguishable(monkeypatch):
    """The defect: 'ran, found no AG' looked exactly like 'never enabled'."""
    ran = FakeStore()
    _run_ha(monkeypatch, HaReportStub(False, notes=["No AG."]), ran)
    never_enabled = FakeStore()          # flag off -> _poll_ha is never called
    assert ran.events != never_enabled.events


def test_posture_is_written_once_not_every_cycle(monkeypatch):
    store = FakeStore()
    report = HaReportStub(False, notes=["No AG."])
    for _ in range(5):
        _run_ha(monkeypatch, report, store)
    assert len([1 for _, t, _ in store.events if t == "ha_posture"]) == 1


def test_posture_change_is_recorded(monkeypatch):
    store = FakeStore()
    # focus on posture recording; lag evaluation has its own test below
    monkeypatch.setattr(poller_mod, "behind_replicas", lambda r, t: [])
    _run_ha(monkeypatch, HaReportStub(False, notes=["No AG."]), store)
    _run_ha(monkeypatch, HaReportStub(True, replicas=[object(), object()]), store)
    postures = [s for _, t, s in store.events if t == "ha_posture"]
    assert len(postures) == 2
    assert "2 replica(s)" in postures[1]


def test_lag_detection_unaffected(monkeypatch):
    """Patch 14 must not change the behaviour it wraps."""
    class R:
        server, role, ag_name = "replica-b", "SECONDARY", "ag1"
        lag_seconds, sync_health, state, sync_state = 120, "NOT_HEALTHY", "ONLINE", "SYNCHRONIZING"
        lag_bytes = 0
    store = FakeStore()
    report = HaReportStub(True, replicas=[R()])
    monkeypatch.setattr(poller_mod, "behind_replicas", lambda r, t: list(r.replicas))
    result = _run_ha(monkeypatch, report, store)
    assert result.get("replica_lag") == 1
    assert any(t == "replica_lag" for _, t, _ in store.events)


def test_ha_failure_still_degrades_to_an_error_event(monkeypatch):
    """A denied DMV read must be recorded, never silently treated as healthy."""
    def boom(adapter):
        raise PermissionError("VIEW SERVER STATE permission was denied")
    monkeypatch.setattr(poller_mod, "collect_ha", boom)
    store = FakeStore()
    result = {"notifications": []}
    poller_mod._poll_ha(store, "db-under-test", object(), Cfg(), FakeNotifier(), result)
    assert [t for _, t, _ in store.events] == ["error"]
    assert "HA check skipped" in store.events[0][2]
    assert "ha_enabled" not in result


# --- v3.3.0: ha_posture is subscribable, not just recorded -----------------

class RecordingNotifier:
    """FakeNotifier's sibling that keeps what it was asked to send."""
    def __init__(self):
        self.sent = []

    def notify(self, event_type, title, text):
        self.sent.append((event_type, title, text))
        return [("slack", True, None)]


def test_ha_posture_is_a_subscribable_event_type():
    """Recording a posture change in the store while leaving it out of
    EVENT_TYPES would be half-wired: `notify.on:` is validated against that
    list, so an operator could not subscribe, and should_notify would refuse it
    forever."""
    from sqldoc.agent.config import EVENT_TYPES, NotifyConfig
    from sqldoc.agent.notify import Notifier

    assert "ha_posture" in EVENT_TYPES
    assert Notifier(NotifyConfig()).should_notify("ha_posture"),         "ha_posture must be on by default like every other event type"


def test_ha_posture_is_accepted_in_the_notify_on_list():
    """Before it was in EVENT_TYPES this config raised at parse time."""
    from sqldoc.agent.config import parse_agent_config

    ac = parse_agent_config({"agent": {
        "databases": [{"name": "prod", "connection_string": "postgresql://u:p@h/db"}],
        "notifications": {"on": ["ha_posture"]}}})
    assert ac.notify.on == ["ha_posture"]


def test_ha_posture_change_is_pushed_not_only_recorded(monkeypatch):
    """A posture change must reach the operator, not just the dashboard."""
    monkeypatch.setattr(poller_mod, "collect_ha",
                        lambda adapter: HaReportStub(
                            False, notes=["No Always On availability groups are "
                                          "configured on this instance."]))
    store, notifier = FakeStore(), RecordingNotifier()
    result = {"notifications": []}
    poller_mod._poll_ha(store, "db-under-test", object(), Cfg(), notifier, result)

    assert [e for e, _, _ in notifier.sent] == ["ha_posture"]
    assert "No Always On" in notifier.sent[0][2]
    assert len(result["notifications"]) == 1


def test_steady_posture_never_notifies_again(monkeypatch):
    """Written only on CHANGE -- so a steady state costs no events and no pages."""
    monkeypatch.setattr(poller_mod, "collect_ha",
                        lambda adapter: HaReportStub(False, notes=["No AG."]))
    store, notifier = FakeStore(), RecordingNotifier()
    for _ in range(5):
        poller_mod._poll_ha(store, "db", object(), Cfg(), notifier,
                            {"notifications": []})
    assert len(notifier.sent) == 1
    assert len([1 for _, t, _ in store.events if t == "ha_posture"]) == 1


# --- v3.3.0: SIMPLE recovery is informational, not a failure ---------------

def _simple_only():
    """The live shape: every user database SIMPLE, `model` FULL."""
    F = "2026-09-01 02:00:00"
    rows = [db_row("master", "SIMPLE", last_full=F), db_row("model", "FULL", last_full=F),
            db_row("msdb", "SIMPLE", last_full=F), db_row("app_one", "SIMPLE", last_full=F),
            db_row("app_two", "SIMPLE", last_full=F)]
    return backup_mod._collect_sqlserver(FakeCursor(rows))


def _by_name(report, name):
    return next(d for d in report.databases if d.database == name)


def test_simple_recovery_is_not_scored_as_a_failure():
    """SIMPLE is a real PITR gap AND a legitimate deliberate config (dev/test,
    read replicas, warehouses reloaded from source). Counting it as broken
    teaches teams who chose it on purpose to ignore the scorecard."""
    app = _by_name(_simple_only(), "app_one")
    assert app.issues == [], "SIMPLE must not be recorded as a failure"


def test_simple_recovery_is_still_surfaced_not_silent():
    """The other half: it must not be hidden either. Silent is the failure mode
    this whole release exists to remove."""
    app = _by_name(_simple_only(), "app_one")
    assert len(app.informational) == 1
    finding = app.informational[0]
    assert "SIMPLE recovery model" in finding
    assert "no point-in-time recovery" in finding
    assert "Confirm this is intended" in finding,         "the finding must ask the DBA to judge, not assert a defect"


def test_simple_recovery_no_longer_depresses_the_compliance_score():
    from sqldoc.executive import backup_compliance
    # 5 databases, only `model` genuinely non-compliant (FULL with no log backups)
    assert backup_compliance(_simple_only()) == 80


def test_a_real_failure_is_still_a_failure():
    """The severity split must not soften anything that IS broken."""
    r = _simple_only()
    model = _by_name(r, "model")
    assert model.issues and "no log backups" in model.issues[0]
    assert model.informational == []


def test_never_backed_up_stays_a_failure_even_on_simple():
    rows = [db_row("app", "SIMPLE", last_full=None)]   # never backed up
    d = backup_mod._collect_sqlserver(FakeCursor(rows)).databases[0]
    assert d.never_backed_up and d.issues, "never-backed-up is a failure regardless"
    assert d.informational, "and the SIMPLE posture is still reported alongside it"


def test_system_databases_raise_neither():
    r = _simple_only()
    for name in ("master", "msdb"):
        d = _by_name(r, name)
        assert d.issues == [] and d.informational == []


def test_the_two_severities_are_reported_separately():
    s = backup_mod.summarize(_simple_only())
    assert s["with_issues"] == 1, "failures count only real failures"
    assert s["with_informational"] == 2, "and the review items are counted too"
