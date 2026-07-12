"""Cover the notify transports (Slack/Teams/Webex/email/SMS) and Notifier
branches directly. No network / SMTP."""
import smtplib

import pytest
import requests

from sqldoc.agent import notify as N
from sqldoc.agent.config import NotifyConfig


class FakeResp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def posts(monkeypatch):
    captured = []
    monkeypatch.setattr(requests, "post",
                        lambda url, **kw: captured.append((url, kw)) or FakeResp(200))
    return captured


def test_send_slack(posts):
    N.send_slack("https://hook", "hi")
    assert posts[0][0] == "https://hook" and posts[0][1]["json"] == {"text": "hi"}


def test_send_teams_card(posts):
    N.send_teams("https://teams", "Title", "line1\nline2", color="2EB67D")
    card = posts[0][1]["json"]
    assert card["@type"] == "MessageCard" and card["themeColor"] == "2EB67D"
    assert "line1\n\nline2" in card["text"]


def test_send_webex(posts):
    N.send_webex({"token": "T", "room_id": "R"}, "Title", "body")
    assert posts[0][0] == "https://webexapis.com/v1/messages"
    assert posts[0][1]["headers"]["Authorization"] == "Bearer T"


def test_send_webex_requires_fields():
    with pytest.raises(ValueError):
        N.send_webex({"token": "T"}, "t", "x")


def test_send_twilio_and_whatsapp(posts):
    N.send_twilio_sms({"account_sid": "AC", "auth_token": "k", "from_number": "+1", "to": "+2"}, "sms")
    N.send_whatsapp({"token": "T", "phone_number_id": "1", "to": "+3"}, "wa")
    assert any("Messages.json" in u for u, _ in posts)
    assert any("/1/messages" in u for u, _ in posts)


def test_sms_gateway_uses_email(monkeypatch):
    sent = []
    monkeypatch.setattr(N, "send_email", lambda smtp, subj, body: sent.append((subj, body)))
    N.send_sms_via_gateway({"smtp_host": "x", "to": ["5551234@vtext.com"]}, "Subj", "long body " * 100)
    assert sent[0][0] == "Subj" and len(sent[0][1]) <= 300


# --- email via smtplib -----------------------------------------------------

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.actions = []
        FakeSMTP.instances.append(self)

    def starttls(self):
        self.actions.append("starttls")

    def login(self, u, p):
        self.actions.append(("login", u))

    def sendmail(self, sender, recipients, msg):
        self.actions.append(("sendmail", sender, tuple(recipients)))

    def quit(self):
        self.actions.append("quit")


def test_send_email(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    N.send_email({"smtp_host": "smtp.x", "smtp_port": 587, "username": "u@x", "password": "p",
                  "from": "from@x", "to": ["a@x", "b@x"], "use_tls": True}, "Subj", "Body")
    inst = FakeSMTP.instances[0]
    assert "starttls" in inst.actions and any(a[0] == "sendmail" for a in inst.actions if isinstance(a, tuple))


def test_send_html_email_multipart(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    N.send_html_email({"smtp_host": "smtp.x", "from": "f@x", "to": "a@x"},
                      "Subj", "<b>hi</b>", text_body="hi")
    assert FakeSMTP.instances[0].actions


def test_email_requires_recipient():
    with pytest.raises(ValueError):
        N.send_email({"smtp_host": "x"}, "s", "b")     # no 'to'


# --- Notifier channel error isolation --------------------------------------

def test_notifier_all_channels_isolated(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    for fn in ("send_slack", "send_teams", "send_webex", "send_twilio_sms",
               "send_whatsapp", "send_sms_via_gateway", "send_email"):
        monkeypatch.setattr(N, fn, boom)
    cfg = NotifyConfig(slack_webhook="s", teams_webhook="t", webex={"token": "T", "room_id": "R"},
                       twilio={"account_sid": "a"}, whatsapp={"token": "t"},
                       sms_gateway={"smtp_host": "x"}, smtp={"smtp_host": "x", "to": ["a"]})
    results = N.Notifier(cfg).notify("disk_low", "t", "x")
    # every channel attempted, every one recorded as a failure (not raised)
    assert len(results) == 7 and all(ok is False for (_c, ok, _e) in results)
