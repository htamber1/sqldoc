"""Scheduled weekly email digest: config, build, render, scheduling."""
from datetime import datetime, timedelta, timezone

import pytest

from sqldoc.agent import weekly, config as agent_config
from sqldoc.agent.store import AgentStore


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "agent.db"))


# --- config parsing ---------------------------------------------------------

def test_weekly_config_bool_true():
    wr = agent_config._parse_weekly(True)
    assert wr.enabled and wr.weekday == 0 and wr.hour == 8


def test_weekly_config_disabled():
    assert agent_config._parse_weekly(None).enabled is False
    assert agent_config._parse_weekly(False).enabled is False


def test_weekly_config_mapping():
    wr = agent_config._parse_weekly({"day": "friday", "hour": 6})
    assert wr.enabled and wr.weekday == 4 and wr.hour == 6


def test_weekly_config_bad_day():
    with pytest.raises(ValueError):
        agent_config._parse_weekly({"day": "someday"})
    with pytest.raises(ValueError):
        agent_config._parse_weekly({"hour": 99})


def test_parse_agent_config_includes_weekly():
    cfg = {"agent": {"databases": [{"name": "db1", "connection_string": "./x.db", "dialect": "sqlite"}],
                     "weekly_report": {"day": "monday", "hour": 9}}}
    ac = agent_config.parse_agent_config(cfg)
    assert ac.weekly_report.enabled and ac.weekly_report.hour == 9


# --- store queries ----------------------------------------------------------

def test_events_since_and_meta(store):
    store.add_event("db1", "schema_change", "added table X")
    store.add_event("db1", "new_pii", "found ssn")
    recent = store.events_since("2000-01-01T00:00:00+00:00", "db1")
    assert len(recent) == 2
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    store.set_meta("k", "w")
    assert store.get_meta("k") == "w"


# --- digest build + render --------------------------------------------------

def test_build_digest_counts(store):
    store.add_event("db1", "schema_change", "x")
    store.add_event("db1", "new_pii", "y")
    store.add_event("db1", "job_failure", "z")
    store.add_event("db1", "disk_low", "w")
    store.add_metric("db1", pii_high=2, pii_score=16, health_issues=5)
    store.add_metric("db1", pii_high=1, pii_score=8, health_issues=3)
    d = weekly.build_weekly_digest(store, ["db1"], "2000-01-01T00:00:00+00:00", "July 12, 2026")
    s = d["databases"][0]
    assert len(s["schema_changes"]) == 1
    assert len(s["new_pii"]) == 1
    assert len(s["job_failures"]) == 1
    assert len(s["infra_alerts"]) == 1
    assert s["pii_score_delta"] == 8 - 16   # improved
    assert s["health_delta"] == 3 - 5
    assert d["totals"]["schema_changes"] == 1


def test_render_html_and_text(store):
    store.add_event("db1", "schema_change", "x")
    d = weekly.build_weekly_digest(store, ["db1"], "2000-01-01T00:00:00+00:00", "July 12, 2026")
    html = weekly.render_weekly_html(d)
    assert "Weekly digest" in html and "db1" in html
    assert "http://" not in html and "https://" not in html   # self-contained
    text = weekly.render_weekly_text(d)
    assert "db1" in text and "Schema changes" in text


# --- scheduling -------------------------------------------------------------

def _wr(day=0, hour=8):
    return agent_config.WeeklyReportConfig(enabled=True, weekday=day, hour=hour)


def test_is_due_matches_day_hour():
    # Monday 2026-07-13 at 09:00
    monday_9 = datetime(2026, 7, 13, 9, 0)
    assert monday_9.weekday() == 0
    assert weekly.is_due(_wr(0, 8), monday_9, last_week_key=None) is True


def test_is_due_before_hour():
    monday_7 = datetime(2026, 7, 13, 7, 0)
    assert weekly.is_due(_wr(0, 8), monday_7, None) is False


def test_is_due_wrong_day():
    tuesday = datetime(2026, 7, 14, 9, 0)
    assert weekly.is_due(_wr(0, 8), tuesday, None) is False


def test_is_due_already_sent_this_week():
    monday_9 = datetime(2026, 7, 13, 9, 0)
    key = weekly._week_key(monday_9)
    assert weekly.is_due(_wr(0, 8), monday_9, last_week_key=key) is False


def test_is_due_disabled():
    off = agent_config.WeeklyReportConfig(enabled=False)
    assert weekly.is_due(off, datetime(2026, 7, 13, 9), None) is False


def test_maybe_send_sends_and_records(store):
    class _AC:
        databases = [type("D", (), {"name": "db1"})()]
        weekly_report = _wr(0, 8)
        notify = type("N", (), {"smtp": {"to": "a@b.c", "smtp_host": "h"}})()

    sent = {}

    def fake_send(smtp, subject, html, text):
        sent.update(subject=subject, html=html)

    monday_9 = datetime(2026, 7, 13, 9, 0)
    ok = weekly.maybe_send_weekly_report(store, _AC(), now=monday_9, send_fn=fake_send)
    assert ok is True
    assert "Weekly digest" in sent["subject"]
    # idempotent: a second call the same week does not resend
    ok2 = weekly.maybe_send_weekly_report(store, _AC(), now=monday_9, send_fn=fake_send)
    assert ok2 is False


def test_maybe_send_skips_without_smtp(store):
    class _AC:
        databases = []
        weekly_report = _wr(0, 8)
        notify = type("N", (), {"smtp": None})()
    assert weekly.maybe_send_weekly_report(store, _AC(), now=datetime(2026, 7, 13, 9)) is False
