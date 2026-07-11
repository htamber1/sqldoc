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
    resp = requests.post(webhook, json={"text": text}, timeout=timeout)
    resp.raise_for_status()


def send_email(smtp: dict, subject: str, body: str):
    """Send a plaintext email via SMTP. `smtp` keys: smtp_host, smtp_port,
    username, password, from, to (str or list), use_tls (default True)."""
    recipients = smtp.get("to")
    if isinstance(recipients, str):
        recipients = [recipients]
    if not recipients:
        raise ValueError("email notification config needs at least one 'to' address.")
    sender = smtp.get("from") or smtp.get("username")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

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

        if self.cfg.smtp:
            try:
                send_email(self.cfg.smtp, f"[sqldoc agent] {title}", text)
                results.append(("email", True, None))
            except Exception as e:
                results.append(("email", False, f"{type(e).__name__}: {e}"))

        return results
