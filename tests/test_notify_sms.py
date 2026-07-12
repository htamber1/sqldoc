"""SMS (Twilio), WhatsApp, and SMTP-to-SMS gateway notification channels."""
import pytest

from sqldoc.agent import notify as notifymod
from sqldoc.agent.notify import (Notifier, send_twilio_sms, send_whatsapp)
from sqldoc.agent.config import NotifyConfig, parse_agent_config


# --- transports ------------------------------------------------------------

def test_send_twilio_posts_per_recipient(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, auth=None, data=None, timeout=None, **k):
        calls.append((url, auth, data))
        return Resp()
    monkeypatch.setattr(notifymod.requests, "post", fake_post)
    send_twilio_sms({"account_sid": "AC1", "auth_token": "tok", "from_number": "+1000",
                     "to": ["+1111", "+2222"]}, "hello")
    assert len(calls) == 2
    assert "Accounts/AC1/Messages.json" in calls[0][0]
    assert calls[0][1] == ("AC1", "tok") and calls[0][2]["Body"] == "hello"


def test_twilio_missing_config():
    with pytest.raises(ValueError):
        send_twilio_sms({"account_sid": "AC1"}, "x")


def test_send_whatsapp(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, headers=None, json=None, timeout=None, **k):
        calls.append((url, headers, json))
        return Resp()
    monkeypatch.setattr(notifymod.requests, "post", fake_post)
    send_whatsapp({"token": "T", "phone_number_id": "123", "to": "+1999"}, "alert!")
    assert "123/messages" in calls[0][0]
    assert calls[0][1]["Authorization"] == "Bearer T"
    assert calls[0][2]["text"]["body"] == "alert!"


# --- Notifier dispatch -----------------------------------------------------

def test_notifier_dispatches_sms_channels(monkeypatch):
    sent = []
    monkeypatch.setattr(notifymod, "send_twilio_sms", lambda cfg, text: sent.append(("sms", text)))
    monkeypatch.setattr(notifymod, "send_whatsapp", lambda cfg, text: sent.append(("wa", text)))
    monkeypatch.setattr(notifymod, "send_sms_via_gateway",
                        lambda cfg, subj, text: sent.append(("gw", subj)))
    n = Notifier(NotifyConfig(twilio={"account_sid": "a"}, whatsapp={"token": "t"},
                              sms_gateway={"smtp_host": "x", "to": ["5551234@vtext.com"]}))
    results = n.notify("disk_low", "PROD low disk", "5% free")
    channels = {r[0] for r in results}
    assert {"sms", "whatsapp", "sms_gateway"} <= channels
    assert all(ok for (_c, ok, _e) in results)


def test_notifier_isolates_sms_failure(monkeypatch):
    def boom(cfg, text):
        raise RuntimeError("twilio down")
    monkeypatch.setattr(notifymod, "send_twilio_sms", boom)
    n = Notifier(NotifyConfig(twilio={"account_sid": "a"}))
    results = n.notify("disk_low", "x", "y")
    assert results == [("sms", False, "RuntimeError: twilio down")]


# --- config parsing --------------------------------------------------------

def test_parse_sms_channels():
    cfg = {"agent": {
        "databases": [{"name": "a", "connection_string": "mysql://u:p@h/db"}],
        "notifications": {
            "twilio": {"account_sid": "AC", "auth_token": "t", "from_number": "+1", "to": ["+2"]},
            "whatsapp": {"token": "T", "phone_number_id": "1", "to": ["+3"]},
            "sms_gateway": {"smtp_host": "smtp", "to": ["5551234@vtext.com"]},
        },
    }}
    ac = parse_agent_config(cfg)
    assert ac.notify.twilio["account_sid"] == "AC"
    assert ac.notify.whatsapp["phone_number_id"] == "1"
    assert ac.notify.sms_gateway["smtp_host"] == "smtp"


# --- AlertManager routing --------------------------------------------------

def test_alertmanager_routes_sms(monkeypatch, tmp_path):
    from sqldoc.agent import alerting
    from sqldoc.agent.alerting import AlertManager, AlertingConfig
    from sqldoc.agent.store import AgentStore
    sent = []
    monkeypatch.setattr(alerting, "send_twilio_sms", lambda cfg, text: sent.append(("sms", text)))
    monkeypatch.setattr(alerting, "send_slack", lambda wh, text: sent.append(("slack", text)))
    store = AgentStore(str(tmp_path / "a.db"))
    cfg = NotifyConfig(slack_webhook="s", twilio={"account_sid": "a"})
    a = AlertingConfig(routing={"critical": ["sms"], "medium": ["slack"]})
    mgr = AlertManager(cfg, store, a, now_fn=lambda: 1000.0)
    mgr.notify("disk_low", "PROD: low disk", "5% free")   # critical -> sms only
    assert ("sms", "[sqldoc] PROD: low disk") in sent
    assert not any(c == "slack" for c, _ in sent)
