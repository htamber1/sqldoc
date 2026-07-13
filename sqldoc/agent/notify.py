"""Notifications for the agent: Slack webhooks and email (SMTP).

Both transports are best-effort and isolated: a failing webhook or mail server
records an error but never crashes a poll. The transport functions are
module-level so tests can monkeypatch them without a real network.
"""
import smtplib
from email.mime.text import MIMEText

import requests


def send_slack(webhook: str, text: str, timeout: float = 10.0):
    """POST a Slack incoming-webhook message."""
    from sqldoc.nethttp import safe_request
    resp = safe_request("POST", webhook, json={"text": text}, timeout=timeout)
    resp.raise_for_status()


def send_teams(webhook: str, title: str, text: str, timeout: float = 10.0,
               color: str = "0076D7"):
    """POST a Microsoft Teams Incoming Webhook message (legacy MessageCard, which
    every Teams connector accepts). Blank lines become Markdown paragraph breaks."""
    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": title or "sqldoc",
        "themeColor": color,
        "title": title,
        "text": (text or "").replace("\n", "\n\n"),
    }
    from sqldoc.nethttp import safe_request
    resp = safe_request("POST", webhook, json=card, timeout=timeout)
    resp.raise_for_status()


def send_webex(config: dict, title: str, text: str, timeout: float = 10.0):
    """Post a message to a Cisco Webex space via the Messages API. `config` keys:
    token (bot/user access token) + room_id (target space). Preferred by
    government / large-enterprise deployments standardised on Webex."""
    token = config.get("token")
    room_id = config.get("room_id")
    if not token or not room_id:
        raise ValueError("webex notification config needs both 'token' and 'room_id'.")
    body = {"roomId": room_id, "markdown": f"**{title}**\n\n{text}"}
    resp = requests.post("https://webexapis.com/v1/messages", json=body,
                         headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    resp.raise_for_status()


def _as_list(v):
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def send_twilio_sms(config: dict, text: str, timeout: float = 10.0):
    """Send an SMS to one or more numbers via the Twilio REST API. `config` keys:
    account_sid, auth_token, from_number, to (str or list of E.164 numbers)."""
    sid = config.get("account_sid")
    token = config.get("auth_token")
    frm = config.get("from_number") or config.get("from")
    recipients = _as_list(config.get("to"))
    if not (sid and token and frm and recipients):
        raise ValueError("twilio config needs account_sid, auth_token, from_number, and to.")
    body = (text or "")[:1500]
    for num in recipients:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token), data={"From": frm, "To": num, "Body": body}, timeout=timeout)
        resp.raise_for_status()


def send_whatsapp(config: dict, text: str, timeout: float = 10.0):
    """Send a WhatsApp message via the Meta WhatsApp Business Cloud API. `config`
    keys: token, phone_number_id, to (str or list), api_version (default v18.0)."""
    token = config.get("token")
    pid = config.get("phone_number_id")
    recipients = _as_list(config.get("to"))
    if not (token and pid and recipients):
        raise ValueError("whatsapp config needs token, phone_number_id, and to.")
    ver = config.get("api_version", "v18.0")
    for num in recipients:
        resp = requests.post(
            f"https://graph.facebook.com/{ver}/{pid}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product": "whatsapp", "to": num, "type": "text",
                  "text": {"body": (text or "")[:4000]}}, timeout=timeout)
        resp.raise_for_status()


def send_sms_via_gateway(config: dict, subject: str, text: str):
    """Send via an SMTP-to-SMS gateway: `config` is an SMTP dict whose `to`
    addresses are the carrier gateway addresses (e.g. 5551234567@vtext.com)."""
    send_email(config, subject, (text or "")[:300])


def _send(smtp: dict, msg, recipients, sender):
    host = smtp.get("smtp_host")
    port = int(smtp.get("smtp_port", 587))
    server = smtplib.SMTP(host, port, timeout=20)
    try:
        if smtp.get("use_tls", True):
            server.starttls()
        if smtp.get("username"):
            server.login(smtp["username"], smtp.get("password", ""))
        server.sendmail(sender, recipients, msg.as_string())
    finally:
        server.quit()


def _prepare(smtp: dict, subject: str):
    recipients = smtp.get("to")
    if isinstance(recipients, str):
        recipients = [recipients]
    if not recipients:
        raise ValueError("email notification config needs at least one 'to' address.")
    sender = smtp.get("from") or smtp.get("username")
    return recipients, sender


def send_email(smtp: dict, subject: str, body: str):
    """Send a plaintext email via SMTP. `smtp` keys: smtp_host, smtp_port,
    username, password, from, to (str or list), use_tls (default True)."""
    recipients, sender = _prepare(smtp, subject)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    _send(smtp, msg, recipients, sender)


def send_html_email(smtp: dict, subject: str, html_body: str, text_body: str = None):
    """Send an HTML email (with an optional plaintext alternative) via SMTP.
    Used for the scheduled weekly digest."""
    from email.mime.multipart import MIMEMultipart
    recipients, sender = _prepare(smtp, subject)
    if text_body:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
    _send(smtp, msg, recipients, sender)


class Notifier:
    """Dispatches an event to the configured channels, honouring the ``on``
    allowlist. Returns a list of (channel, ok, error) results per notify()."""

    def __init__(self, notify_config):
        self.cfg = notify_config

    def should_notify(self, event_type: str) -> bool:
        return event_type in (self.cfg.on or [])

    def notify(self, event_type: str, title: str, text: str) -> list:
        results = []
        if not self.should_notify(event_type):
            return results
        message = f"[sqldoc agent] {title}\n{text}"

        if self.cfg.slack_webhook:
            try:
                send_slack(self.cfg.slack_webhook, message)
                results.append(("slack", True, None))
            except Exception as e:
                results.append(("slack", False, f"{type(e).__name__}: {e}"))

        if getattr(self.cfg, "teams_webhook", None):
            try:
                # Colour document updates blue-green, everything else red-orange.
                color = "2EB67D" if event_type == "doc_updated" else "D93F0B"
                send_teams(self.cfg.teams_webhook, f"[sqldoc] {title}", text, color=color)
                results.append(("teams", True, None))
            except Exception as e:
                results.append(("teams", False, f"{type(e).__name__}: {e}"))

        if getattr(self.cfg, "webex", None):
            try:
                send_webex(self.cfg.webex, f"[sqldoc] {title}", text)
                results.append(("webex", True, None))
            except Exception as e:
                results.append(("webex", False, f"{type(e).__name__}: {e}"))

        sms_text = f"[sqldoc] {title}"
        if getattr(self.cfg, "twilio", None):
            try:
                send_twilio_sms(self.cfg.twilio, sms_text)
                results.append(("sms", True, None))
            except Exception as e:
                results.append(("sms", False, f"{type(e).__name__}: {e}"))

        if getattr(self.cfg, "whatsapp", None):
            try:
                send_whatsapp(self.cfg.whatsapp, f"[sqldoc] {title}\n{text}")
                results.append(("whatsapp", True, None))
            except Exception as e:
                results.append(("whatsapp", False, f"{type(e).__name__}: {e}"))

        if getattr(self.cfg, "sms_gateway", None):
            try:
                send_sms_via_gateway(self.cfg.sms_gateway, f"[sqldoc] {title}", text)
                results.append(("sms_gateway", True, None))
            except Exception as e:
                results.append(("sms_gateway", False, f"{type(e).__name__}: {e}"))

        if self.cfg.smtp:
            try:
                send_email(self.cfg.smtp, f"[sqldoc agent] {title}", text)
                results.append(("email", True, None))
            except Exception as e:
                results.append(("email", False, f"{type(e).__name__}: {e}"))

        return results
