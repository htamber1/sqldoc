"""Live validation of the notification channels.

The agent's alert channels (Slack/Teams/Webex/email/SMS/WhatsApp and the
PagerDuty/OpsGenie incident channels) are transport-mocked in the unit suite.
Each test here sends a **real** test message through a configured channel, so a
developer can confirm delivery in their own workspace/inbox/pager.

Config lives under a ``live_notify:`` section in the live config (see
``tests/live/sqldoc.live.example.yml``). Only the sub-keys you fill in run; the
rest skip. **These send real messages** — expect a "sqldoc live test" alert in
each configured destination.
"""
import pytest

from _liveutil import live_config

pytestmark = pytest.mark.live

_MSG = "sqldoc live test — validating this notification channel. Safe to ignore."


def _cfg(subkey):
    return (live_config().get("live_notify") or {}).get(subkey)


def _need(subkey):
    val = _cfg(subkey)
    if not val:
        pytest.skip(f"add live_notify.{subkey} to the live config to test this channel")
    return val


def test_slack():
    webhook = _need("slack_webhook")
    from sqldoc.agent.notify import send_slack
    send_slack(webhook, _MSG)
    print("\n[slack] posted — check the channel")


def test_teams():
    webhook = _need("teams_webhook")
    from sqldoc.agent.notify import send_teams
    send_teams(webhook, "sqldoc live test", _MSG)
    print("\n[teams] posted — check the channel")


def test_webex():
    cfg = _need("webex")
    from sqldoc.agent.notify import send_webex
    send_webex(cfg, "sqldoc live test", _MSG)
    print("\n[webex] posted — check the space")


def test_email():
    smtp = _need("email")
    from sqldoc.agent.notify import send_email
    send_email(smtp, "sqldoc live test", _MSG)
    print("\n[email] sent — check the inbox")


def test_twilio_sms():
    cfg = _need("twilio")
    from sqldoc.agent.notify import send_twilio_sms
    send_twilio_sms(cfg, _MSG)
    print("\n[twilio] SMS sent — check the phone")


def test_whatsapp():
    cfg = _need("whatsapp")
    from sqldoc.agent.notify import send_whatsapp
    send_whatsapp(cfg, _MSG)
    print("\n[whatsapp] message sent — check the phone")


def test_pagerduty():
    cfg = _need("pagerduty")
    from sqldoc.agent.alerting import send_pagerduty
    send_pagerduty(cfg, _MSG, severity="low", source="sqldoc-live-test",
                   dedup_key="sqldoc-live-test")
    print("\n[pagerduty] event enqueued — check PagerDuty (then resolve it)")


def test_opsgenie():
    cfg = _need("opsgenie")
    from sqldoc.agent.alerting import send_opsgenie
    send_opsgenie(cfg, _MSG, severity="low", alias="sqldoc-live-test",
                  description="sqldoc live channel validation")
    print("\n[opsgenie] alert created — check OpsGenie (then close it)")
