"""Regression tests for escalation delivery accounting (v3.2.0, patch 19).

Found while exercising the real notification channels against a local sink.
Every channel formatted and sent correctly; the defect was one layer up, in the
path that re-sends an unacknowledged alert to a second tier.

PATCH 19 -- `run_escalations` discarded `_dispatch`'s results.

    self._dispatch(tier2, ...)                  # <-- return value thrown away
    self.store.mark_alert(al["id"], "escalated")
    log(f"escalated alert #{...} to {', '.join(tier2) or 'no channels'}")
    n += 1

`_dispatch` returns per-channel `(channel, ok, error)` and swallows every
exception, so it returns normally whether the page was delivered or not. Three
scenarios were therefore INDISTINGUISHABLE -- same return count, same stored
`escalated` status, same log line:

    A. `escalation.channels` names a channel that is not configured, so
       `tier2` is empty and `_dispatch([])` sends nothing at all;
    B. every tier-2 channel raises (network refused, 500, bad credential);
    C. the escalation genuinely paged the on-call.

Worse than the misreport: the alert was marked `escalated` regardless, which
drops it out of `pending_escalations()` forever. A critical alert that nobody
acknowledged, whose escalation reached nobody, stops escalating and is recorded
as having been escalated.

This is the general defect recorded as PH-2 -- a step that could not run
reporting the same thing as a step that succeeded -- landing in the one path
that exists specifically for the alert nobody has looked at.

The fix: keep the dispatch results; mark `escalated` only when at least one
tier-2 channel actually accepted it, `escalation_failed` otherwise; record an
error event naming the failed channels (or the empty-tier-2 misconfiguration);
count only delivered escalations; and log the two outcomes differently.

Run with:  pytest test_escalation_delivery.py
"""
import pytest

from sqldoc.agent import alerting as alerting_mod
from sqldoc.agent.alerting import AlertManager, AlertingConfig
from sqldoc.agent.config import NotifyConfig


# --- fakes -----------------------------------------------------------------

class FakeStore:
    """Just enough store for the alerting paths."""

    def __init__(self):
        self.alerts = {}
        self.events = []
        self._next = 1

    def add_alert(self, db_name, type, severity, summary, detail=None,
                  status="fired", channels="", dedup_key=None, escalate_at=None,
                  at_epoch=None):
        aid = self._next
        self._next += 1
        self.alerts[aid] = {"id": aid, "db_name": db_name, "type": type,
                            "severity": severity, "summary": summary,
                            "detail": detail, "status": status,
                            "channels": channels, "dedup_key": dedup_key,
                            "escalate_at": escalate_at, "at_epoch": at_epoch}
        return aid

    def recent_alert_since(self, dedup_key, since_epoch):
        return None

    def pending_escalations(self, now_epoch):
        return [a for a in self.alerts.values()
                if a["status"] == "fired" and a["escalate_at"] is not None
                and a["escalate_at"] <= now_epoch]

    def mark_alert(self, alert_id, status, clear_escalation=True):
        self.alerts[alert_id]["status"] = status
        if clear_escalation:
            self.alerts[alert_id]["escalate_at"] = None

    def add_event(self, db_name, type, summary, detail=None):
        self.events.append({"db_name": db_name, "type": type,
                            "summary": summary, "detail": detail})

    # convenience
    @property
    def statuses(self):
        return [a["status"] for a in self.alerts.values()]

    def error_events(self):
        return [e for e in self.events if e["type"] == "error"]


def notify_cfg(**kw):
    return NotifyConfig(on=["disk_low"], **kw)


def make(monkeypatch, *, tier2, slack=True, teams=True,
         slack_ok=True, teams_ok=True):
    """AlertManager whose channel transports are stubbed, plus its store."""
    sent = []

    def slack_fn(webhook, message, **kw):
        sent.append(("slack", message))
        if not slack_ok:
            raise RuntimeError("slack transport failed")

    def teams_fn(webhook, title, text, **kw):
        sent.append(("teams", title))
        if not teams_ok:
            raise RuntimeError("teams transport failed")

    monkeypatch.setattr(alerting_mod, "send_slack", slack_fn)
    monkeypatch.setattr(alerting_mod, "send_teams", teams_fn)

    cfg = notify_cfg(
        slack_webhook="https://example.invalid/slack" if slack else None,
        teams_webhook="https://example.invalid/teams" if teams else None)
    a = AlertingConfig(escalation_after_minutes=30,
                       escalation_severities=["critical"],
                       escalation_channels=list(tier2),
                       routing={"critical": ["slack"]})
    store = FakeStore()
    am = AlertManager(cfg, store, a, now_fn=lambda: 1000.0)
    return am, store, sent


def fire_and_escalate(am, store):
    am.notify("disk_low", "TESTDB01: low disk", "detail")
    am._now = lambda: 1000.0 + 3600
    log_lines = []
    n = am.run_escalations(log=log_lines.append)
    return n, log_lines


# --- the three scenarios that were indistinguishable -----------------------

def test_unconfigured_tier2_is_not_reported_as_escalated(monkeypatch):
    """A. escalation.channels names a channel that is not configured, so
    nothing is dispatched at all."""
    am, store, sent = make(monkeypatch, tier2=["teams"], teams=False)
    n, log_lines = fire_and_escalate(am, store)
    assert n == 0, "an escalation that sent nothing was counted as delivered"
    assert store.statuses == ["escalation_failed"]
    assert not any(ch == "teams" for ch, _ in sent)
    assert store.error_events(), "the misconfiguration was not recorded"
    assert "no tier-2 channel is configured" in log_lines[0]


def test_all_tier2_channels_failing_is_not_reported_as_escalated(monkeypatch):
    """B. THE DANGEROUS ONE. Every tier-2 channel raises; before the patch this
    was byte-identical to a successful page."""
    am, store, sent = make(monkeypatch, tier2=["teams"], teams_ok=False)
    n, log_lines = fire_and_escalate(am, store)
    assert n == 0
    assert store.statuses == ["escalation_failed"]
    assert ("teams", "[sqldoc] [ESCALATED] TESTDB01: low disk") in sent, \
        "the send should still have been ATTEMPTED"
    errs = store.error_events()
    assert errs and "teams" in errs[0]["summary"]
    assert "ESCALATION FAILED" in log_lines[0]


def test_successful_escalation_is_reported_as_escalated(monkeypatch):
    """C. The control. Must keep working exactly as before."""
    am, store, sent = make(monkeypatch, tier2=["teams"])
    n, log_lines = fire_and_escalate(am, store)
    assert n == 1
    assert store.statuses == ["escalated"]
    assert ("teams", "[sqldoc] [ESCALATED] TESTDB01: low disk") in sent
    assert store.error_events() == []
    assert "escalated alert #1" in log_lines[0]
    assert "FAILED" not in log_lines[0]


def test_the_three_outcomes_are_distinguishable(monkeypatch):
    """The whole point: A, B and C must not look the same to a caller."""
    outcomes = []
    for kw in ({"tier2": ["teams"], "teams": False},
               {"tier2": ["teams"], "teams_ok": False},
               {"tier2": ["teams"]}):
        am, store, _ = make(monkeypatch, **kw)
        n, log_lines = fire_and_escalate(am, store)
        outcomes.append((n, tuple(store.statuses), log_lines[0]))
    assert len({o[:2] for o in outcomes}) == 2, \
        "delivered vs undelivered must differ in return value + stored status"
    assert len({o[2] for o in outcomes}) == 3, "each log line must be distinct"


# --- partial delivery ------------------------------------------------------

def test_partial_delivery_counts_as_escalated_but_records_the_failure(monkeypatch):
    """One tier-2 channel delivering is a real page, so it counts -- but the
    channel that failed must not vanish silently."""
    am, store, sent = make(monkeypatch, tier2=["slack", "teams"], teams_ok=False)
    n, log_lines = fire_and_escalate(am, store)
    assert n == 1
    assert store.statuses == ["escalated"]
    errs = store.error_events()
    assert errs, "the failed channel was not recorded"
    assert "teams" in errs[0]["summary"]
    assert "FAILED: teams" in log_lines[0]


def test_escalation_attempts_every_tier2_channel_despite_one_failing(monkeypatch):
    """Isolation: a failing channel must not stop the others."""
    am, store, sent = make(monkeypatch, tier2=["slack", "teams"], slack_ok=False)
    fire_and_escalate(am, store)
    escalated = [ch for ch, msg in sent if "[ESCALATED]" in msg]
    assert sorted(escalated) == ["slack", "teams"]


def test_nothing_pending_is_a_no_op(monkeypatch):
    """Regression: no pending escalations must not record anything."""
    am, store, sent = make(monkeypatch, tier2=["teams"])
    am.a.escalation_after_minutes = 0          # escalation disabled
    am.notify("disk_low", "TESTDB01: low disk", "detail")
    log_lines = []
    assert am.run_escalations(log=log_lines.append) == 0
    assert log_lines == []
    assert store.error_events() == []


# --- v3.3.0: the escalation failure must itself reach someone --------------

def test_failed_escalation_is_pushed_not_only_stored(monkeypatch):
    """An "error" event is NOT in EVENT_TYPES, so recording the failure there
    alone reaches nobody who is not already watching the dashboard -- leaving
    the operator whose escalation path is wired to nothing exactly as
    uninformed as before patch 19. It must be dispatched."""
    am, store, sent = make(monkeypatch, tier2=["teams"], teams=False)   # scenario A
    fire_and_escalate(am, store)

    assert store.statuses == ["escalation_failed"]
    assert store.error_events(), "the store event must still be recorded"
    # ...and a message must actually have gone out about the FAILURE itself,
    # over and above the original tier-1 alert.
    failure_msgs = [m for ch, m in sent if "ESCALATION FAILED" in str(m)]
    assert failure_msgs, "the escalation failure reached no channel"


def test_failed_escalation_notice_skips_the_channels_that_just_failed(monkeypatch):
    """Re-sending the report down a dead channel is how it gets lost."""
    am, store, sent = make(monkeypatch, tier2=["teams"], teams_ok=False)
    sent.clear()
    fire_and_escalate(am, store)

    assert store.statuses == ["escalation_failed"]
    failure = [(ch, m) for ch, m in sent if "ESCALATION FAILED" in str(m)]
    assert failure, "the escalation failure reached no channel"
    assert all(ch != "teams" for ch, _ in failure),         "the notice must not be sent down the channel that just failed"


def test_a_successful_escalation_sends_no_failure_notice(monkeypatch):
    am, store, sent = make(monkeypatch, tier2=["slack"])
    fire_and_escalate(am, store)
    assert store.statuses == ["escalated"]
    assert not [m for _, m in sent if "ESCALATION FAILED" in str(m)]


def test_failure_notice_cannot_storm(monkeypatch):
    """mark_alert clears escalate_at, so the alert leaves pending_escalations
    for good -- at most ONE failure notice per alert, ever."""
    am, store, sent = make(monkeypatch, tier2=["teams"], teams=False)
    fire_and_escalate(am, store)
    for _ in range(5):
        am.run_escalations(log=lambda *a: None)
    assert len([m for _, m in sent if "ESCALATION FAILED" in str(m)]) == 1


def test_escalation_failed_still_counts_for_dedup():
    """That alert DID fire and tier-1 delivery succeeded -- only its escalation
    failed. Dropping it out of the dedup window would re-alert the same
    condition at full rate on every cycle."""
    from sqldoc.agent.store import AgentStore
    import tempfile, os

    d = tempfile.mkdtemp()
    st = AgentStore(os.path.join(d, "agent.db"))
    aid = st.add_alert("db", "disk_low", "critical", "low disk",
                       dedup_key="db|disk_low", at_epoch=1000.0)
    st.mark_alert(aid, "escalation_failed")
    assert st.recent_alert_since("db|disk_low", 900.0) is not None,         "an escalation_failed alert must still suppress a duplicate"


def test_escalation_failed_is_a_subscribable_event_type():
    from sqldoc.agent.config import EVENT_TYPES
    from sqldoc.agent.alerting import DEFAULT_SEVERITY

    assert "escalation_failed" in EVENT_TYPES
    assert DEFAULT_SEVERITY["escalation_failed"] == "critical",         "the failure of the alerting safety net must outrank what it carried"
