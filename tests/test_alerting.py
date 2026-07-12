"""Enterprise alert management: severity routing, maintenance windows, dedup,
escalation, and the incident channels. All senders monkeypatched (no network)."""
from datetime import datetime

import pytest

from sqldoc.agent import alerting
from sqldoc.agent.alerting import AlertManager, AlertingConfig, parse_alerting, in_maintenance
from sqldoc.agent.config import NotifyConfig
from sqldoc.agent.store import AgentStore


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "agent.db"))


@pytest.fixture(autouse=True)
def capture_channels(monkeypatch):
    sent = []
    monkeypatch.setattr(alerting, "send_slack", lambda wh, text: sent.append(("slack", text)))
    monkeypatch.setattr(alerting, "send_teams", lambda wh, t, x, color="0": sent.append(("teams", t)))
    monkeypatch.setattr(alerting, "send_webex", lambda cfg, t, x: sent.append(("webex", t)))
    monkeypatch.setattr(alerting, "send_email", lambda cfg, s, b: sent.append(("email", s)))
    monkeypatch.setattr(alerting, "send_pagerduty",
                        lambda cfg, s, sev, src, dk=None: sent.append(("pagerduty", s, sev, dk)))
    monkeypatch.setattr(alerting, "send_opsgenie",
                        lambda cfg, m, sev, alias=None, desc="": sent.append(("opsgenie", m, sev, alias)))
    monkeypatch.setattr(alerting, "send_servicenow",
                        lambda cfg, et, sev, t, x, db=None: sent.append(("servicenow", t)))
    return sent


# --- config parsing --------------------------------------------------------

def test_parse_alerting_full():
    cfg = {"alerting": {
        "dedup_minutes": 30,
        "severity_overrides": {"schema_change": "high"},
        "routing": {"critical": ["pagerduty", "teams"], "medium": ["slack"]},
        "maintenance_windows": [{"day": "saturday", "start": "22:00", "end": "23:59"}],
        "escalation": {"after_minutes": 15, "severities": ["critical"], "channels": ["pagerduty"]},
        "pagerduty": {"routing_key": "K"},
        "opsgenie": {"api_key": "G"},
    }}
    a = parse_alerting(cfg)
    assert a.dedup_minutes == 30
    assert a.severity_for("schema_change") == "high"
    assert a.channels_for("critical", {"pagerduty", "teams", "slack"}) == ["pagerduty", "teams"]
    assert a.escalates("critical") and not a.escalates("high")


def test_parse_alerting_absent():
    assert parse_alerting({}) is None


def test_default_severity():
    a = AlertingConfig()
    assert a.severity_for("disk_low") == "critical"
    assert a.severity_for("schema_change") == "medium"
    assert a.severity_for("unknown") == "medium"


# --- maintenance windows ---------------------------------------------------

def test_in_maintenance_recurring():
    win = [{"day": "friday", "start": "22:00", "end": "23:30"}]
    fri_2230 = datetime(2026, 7, 17, 22, 30)   # a Friday
    thu_2230 = datetime(2026, 7, 16, 22, 30)   # a Thursday
    assert in_maintenance(win, fri_2230) is True
    assert in_maintenance(win, thu_2230) is False


def test_in_maintenance_absolute():
    win = [{"start": "2026-07-20T00:00", "end": "2026-07-20T06:00"}]
    assert in_maintenance(win, datetime(2026, 7, 20, 3, 0)) is True
    assert in_maintenance(win, datetime(2026, 7, 20, 7, 0)) is False


def test_in_maintenance_everyday():
    win = [{"start": "01:00", "end": "02:00"}]   # no day -> every day
    assert in_maintenance(win, datetime(2026, 7, 15, 1, 30)) is True


# --- routing + dispatch ----------------------------------------------------

def _mgr(store, notify=None, alert=None, now=1000.0):
    notify = notify or NotifyConfig(slack_webhook="s", teams_webhook="t")
    return AlertManager(notify, store, alert or AlertingConfig(), now_fn=lambda: now)


def test_routes_by_severity(store, capture_channels):
    notify = NotifyConfig(slack_webhook="s", teams_webhook="t")
    a = AlertingConfig(routing={"critical": ["pagerduty", "teams"], "medium": ["slack"]},
                       pagerduty={"routing_key": "K"})
    m = _mgr(store, notify, a)
    m.notify("disk_low", "PROD: low disk", "5% free")       # critical
    chans = {c[0] for c in capture_channels}
    assert "pagerduty" in chans and "teams" in chans and "slack" not in chans
    # recorded to history as fired
    hist = store.alerts_since_days(30)
    assert hist[0]["status"] == "fired" and hist[0]["severity"] == "critical"


def test_default_routing_all_configured(store, capture_channels):
    m = _mgr(store)     # no routing -> all configured channels
    m.notify("schema_change", "DB: schema changed", "1 table added")
    chans = {c[0] for c in capture_channels}
    assert chans == {"slack", "teams"}


def test_maintenance_suppresses(store, capture_channels):
    a = AlertingConfig(maintenance_windows=[{"start": "00:00", "end": "23:59"}])
    # now_fn returns an epoch inside the all-day window
    m = AlertManager(NotifyConfig(slack_webhook="s"), store, a,
                     now_fn=lambda: datetime(2026, 7, 15, 12, 0).timestamp())
    res = m.notify("disk_low", "PROD: low disk", "x")
    assert res == [] and not capture_channels
    assert store.alerts_since_days(30)[0]["status"] == "suppressed_maintenance"


def test_dedup_suppresses_repeat(store, capture_channels):
    a = AlertingConfig(dedup_minutes=60)
    m = _mgr(store, alert=a, now=1000.0)
    m.notify("job_failure", "PROD: job failed", "step 2")
    # a second identical alert 10 min later is suppressed
    m2 = _mgr(store, alert=a, now=1000.0 + 600)
    res = m2.notify("job_failure", "PROD: job failed again", "step 2")
    assert res == []
    statuses = [a_["status"] for a_ in store.alerts_since_days(30)]
    assert "suppressed_dedup" in statuses
    # after the window it fires again
    m3 = _mgr(store, alert=a, now=1000.0 + 3600 + 60)
    assert m3.notify("job_failure", "PROD: job failed once more", "step 2")


def test_escalation(store, capture_channels):
    a = AlertingConfig(
        routing={"critical": ["slack"]},
        escalation_after_minutes=15, escalation_severities=["critical"],
        escalation_channels=["pagerduty"], pagerduty={"routing_key": "K"})
    m = _mgr(store, alert=a, now=1000.0)
    m.notify("disk_low", "PROD: low disk", "5% free")
    # not yet due
    m._now = lambda: 1000.0 + 600
    assert m.run_escalations(log=lambda *x: None) == 0
    # past the 15-min mark
    m._now = lambda: 1000.0 + 16 * 60
    assert m.run_escalations(log=lambda *x: None) == 1
    assert any(c[0] == "pagerduty" for c in capture_channels)
    assert store.alerts_since_days(30)[0]["status"] == "escalated"


def test_channel_failure_isolated(store, monkeypatch, capture_channels):
    def boom(wh, text):
        raise RuntimeError("down")
    monkeypatch.setattr(alerting, "send_slack", boom)
    m = _mgr(store, NotifyConfig(slack_webhook="s"))
    results = m.notify("schema_change", "DB: changed", "x")
    assert ("slack", False, "RuntimeError: down") in results
    # still recorded, with no successful channels
    assert store.alerts_since_days(30)[0]["channels"] == ""


def test_respects_on_allowlist(store, capture_channels):
    m = _mgr(store, NotifyConfig(slack_webhook="s", on=["schema_change"]))
    assert m.notify("new_pii", "DB: new pii", "x") == []
    assert not capture_channels
