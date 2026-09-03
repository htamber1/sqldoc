"""Enterprise alert management for the agent.

Layers on top of the plain :class:`~sqldoc.agent.notify.Notifier`:

* **severity classification** per event type (overridable);
* **severity routing** — which channels a severity goes to;
* **maintenance windows** — suppress (but still record) alerts during planned
  downtime, recurring weekly or one-off absolute;
* **deduplication** — collapse repeats of the same (database, event) within a
  window so a flapping condition doesn't spam;
* **escalation paths** — re-notify a tier-2 channel set if a critical/high alert
  is still open after N minutes;
* **PagerDuty / OpsGenie / ServiceNow** incident channels alongside Slack /
  Teams / Webex / email;
* **30-day history** recorded to the store (surfaced on the dashboard).

:class:`AlertManager` is a drop-in for ``Notifier`` — ``notify(event_type,
title, text)`` keeps the same signature, so the poller is unchanged.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqldoc.agent.notify import (
    Notifier, send_slack, send_teams, send_webex, send_email,
    send_twilio_sms, send_whatsapp, send_sms_via_gateway,
)

# Default severity per event type. Overridable via alerting.severity_overrides.
DEFAULT_SEVERITY = {
    "disk_low": "critical",
    "errorlog_critical": "critical",
    "job_failure": "high",
    "backup_stale": "high",
    "replica_lag": "high",
    "linked_server_down": "high",
    "new_pii": "high",
    "tempdb_version_store": "medium",
    "schema_change": "medium",
    "health_degradation": "medium",
    "nl_alert": "medium",
    "doc_updated": "info",
    "ha_posture": "info",
    # Not "high": if the escalation path is broken, the thing that tells you so
    # must outrank the alert whose escalation failed.
    "escalation_failed": "critical",
}
_ALL_CHANNELS = ("slack", "teams", "webex", "email", "sms", "whatsapp", "sms_gateway",
                 "pagerduty", "opsgenie", "servicenow")
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


@dataclass
class AlertingConfig:
    dedup_minutes: float = 0.0          # 0 disables dedup
    severity_overrides: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)       # severity -> [channels]
    maintenance_windows: list = field(default_factory=list)
    escalation_after_minutes: float = 0.0             # 0 disables escalation
    escalation_severities: list = field(default_factory=lambda: ["critical", "high"])
    escalation_channels: list = field(default_factory=list)
    pagerduty: dict = None
    opsgenie: dict = None
    servicenow: dict = None

    def severity_for(self, event_type: str) -> str:
        return self.severity_overrides.get(event_type,
                                           DEFAULT_SEVERITY.get(event_type, "medium"))

    def channels_for(self, severity: str, configured: set) -> list:
        """Channels for a severity: the routing map if given for this severity,
        else every configured channel."""
        routed = self.routing.get(severity)
        if routed is None:
            return [c for c in _ALL_CHANNELS if c in configured]
        return [c for c in routed if c in configured]

    def escalates(self, severity: str) -> bool:
        return (self.escalation_after_minutes > 0 and bool(self.escalation_channels)
                and severity in self.escalation_severities)


def parse_alerting(cfg: dict):
    """Parse the top-level ``alerting:`` section, or None if absent."""
    raw = (cfg or {}).get("alerting")
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("The 'alerting:' config must be a mapping.")
    esc = raw.get("escalation") or {}
    ac = AlertingConfig(
        dedup_minutes=float(raw.get("dedup_minutes", 0) or 0),
        severity_overrides=dict(raw.get("severity_overrides") or {}),
        routing={k: list(v) for k, v in (raw.get("routing") or {}).items()},
        maintenance_windows=list(raw.get("maintenance_windows") or []),
        escalation_after_minutes=float(esc.get("after_minutes", 0) or 0),
        escalation_severities=list(esc.get("severities", ["critical", "high"])),
        escalation_channels=list(esc.get("channels") or []),
        pagerduty=raw.get("pagerduty"),
        opsgenie=raw.get("opsgenie"),
        servicenow=raw.get("servicenow"),
    )
    return ac


# --- maintenance windows ---------------------------------------------------

def _parse_hhmm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def in_maintenance(windows, now: datetime) -> bool:
    """True if `now` (a naive/local datetime) falls in any maintenance window.
    Recurring window: {day: friday, start: "22:00", end: "23:59"} (day optional
    -> every day). Absolute window: {start: "2026-07-20T00:00", end: "...T06:00"}."""
    minute_of_day = now.hour * 60 + now.minute
    for w in windows or []:
        start, end = w.get("start"), w.get("end")
        if not start or not end:
            continue
        if "T" in str(start) or "-" in str(start):
            try:
                s = datetime.fromisoformat(str(start))
                e = datetime.fromisoformat(str(end))
            except ValueError:
                continue
            if s <= now <= e:
                return True
            continue
        day = w.get("day")
        if day is not None:
            wd = _WEEKDAYS.get(str(day).strip().lower())
            if wd is not None and now.weekday() != wd:
                continue
        try:
            if _parse_hhmm(start) <= minute_of_day <= _parse_hhmm(end):
                return True
        except (ValueError, AttributeError):
            continue
    return False


# --- enterprise incident channels (module-level for mocking) ---------------

_PD_SEVERITY = {"critical": "critical", "high": "error", "medium": "warning",
                "low": "info", "info": "info"}
_OG_PRIORITY = {"critical": "P1", "high": "P2", "medium": "P3", "low": "P4", "info": "P5"}


def send_pagerduty(cfg: dict, summary: str, severity: str, source: str,
                   dedup_key: str = None, timeout: float = 10.0):
    """Trigger a PagerDuty Events API v2 alert (native dedup via dedup_key)."""
    import requests
    body = {
        "routing_key": cfg["routing_key"],
        "event_action": "trigger",
        "payload": {
            "summary": summary[:1024],
            "severity": _PD_SEVERITY.get(severity, "warning"),
            "source": source or "sqldoc",
            "component": "sqldoc",
        },
    }
    if dedup_key:
        body["dedup_key"] = dedup_key
    resp = requests.post("https://events.pagerduty.com/v2/enqueue", json=body, timeout=timeout)
    resp.raise_for_status()


def send_opsgenie(cfg: dict, message: str, severity: str, alias: str = None,
                  description: str = "", timeout: float = 10.0):
    """Create an OpsGenie alert (Alerts API v2; alias gives native dedup)."""
    import requests
    host = "api.eu.opsgenie.com" if str(cfg.get("region", "")).lower() == "eu" else "api.opsgenie.com"
    body = {"message": message[:130], "priority": _OG_PRIORITY.get(severity, "P3"),
            "description": description}
    if alias:
        body["alias"] = alias
    resp = requests.post(f"https://{host}/v2/alerts", json=body,
                         headers={"Authorization": f"GenieKey {cfg['api_key']}"}, timeout=timeout)
    resp.raise_for_status()


def send_servicenow(cfg: dict, event_type: str, severity: str, title: str,
                    text: str, database: str = None):
    """Open a ServiceNow incident for an alert (reuses the Table API connector)."""
    from sqldoc.integrations.servicenow import Client
    from sqldoc.integrations.base import FindingEvent
    Client(cfg).create_incident(
        FindingEvent(kind=event_type, severity=severity, title=title, detail=text,
                     database=database or ""))


class AlertManager(Notifier):
    """Notifier + severity routing + maintenance windows + dedup + escalation +
    enterprise incident channels, recording every alert to the store."""

    def __init__(self, notify_config, store, alerting_config=None, now_fn=None):
        super().__init__(notify_config)
        self.store = store
        self.a = alerting_config or AlertingConfig()
        self._now = now_fn or time.time

    def _configured_channels(self) -> set:
        s = set()
        if self.cfg.slack_webhook:
            s.add("slack")
        if getattr(self.cfg, "teams_webhook", None):
            s.add("teams")
        if getattr(self.cfg, "webex", None):
            s.add("webex")
        if getattr(self.cfg, "twilio", None):
            s.add("sms")
        if getattr(self.cfg, "whatsapp", None):
            s.add("whatsapp")
        if getattr(self.cfg, "sms_gateway", None):
            s.add("sms_gateway")
        if self.cfg.smtp:
            s.add("email")
        if self.a.pagerduty:
            s.add("pagerduty")
        if self.a.opsgenie:
            s.add("opsgenie")
        if self.a.servicenow:
            s.add("servicenow")
        return s

    def _send_channel(self, channel, event_type, severity, title, text, dedup_key, db):
        message = f"[sqldoc agent] {title}\n{text}"
        if channel == "slack":
            send_slack(self.cfg.slack_webhook, message)
        elif channel == "teams":
            color = "2EB67D" if event_type == "doc_updated" else "D93F0B"
            send_teams(self.cfg.teams_webhook, f"[sqldoc] {title}", text, color=color)
        elif channel == "webex":
            send_webex(self.cfg.webex, f"[sqldoc] {title}", text)
        elif channel == "email":
            send_email(self.cfg.smtp, f"[sqldoc agent] {title}", text)
        elif channel == "sms":
            send_twilio_sms(self.cfg.twilio, f"[sqldoc] {title}")
        elif channel == "whatsapp":
            send_whatsapp(self.cfg.whatsapp, f"[sqldoc] {title}\n{text}")
        elif channel == "sms_gateway":
            send_sms_via_gateway(self.cfg.sms_gateway, f"[sqldoc] {title}", text)
        elif channel == "pagerduty":
            send_pagerduty(self.a.pagerduty, title, severity, db, dedup_key)
        elif channel == "opsgenie":
            send_opsgenie(self.a.opsgenie, title, severity, dedup_key, text)
        elif channel == "servicenow":
            send_servicenow(self.a.servicenow, event_type, severity, title, text, db)
        else:
            raise ValueError(f"unknown channel '{channel}'")

    def _dispatch(self, channels, event_type, severity, title, text, dedup_key, db) -> list:
        results = []
        for ch in channels:
            try:
                self._send_channel(ch, event_type, severity, title, text, dedup_key, db)
                results.append((ch, True, None))
            except Exception as e:
                results.append((ch, False, f"{type(e).__name__}: {e}"))
        return results

    def notify(self, event_type: str, title: str, text: str) -> list:
        if not self.should_notify(event_type):
            return []
        severity = self.a.severity_for(event_type)
        db = title.split(":", 1)[0].strip() if ":" in title else None
        dedup_key = f"{db}|{event_type}"
        now = self._now()

        # 1) maintenance window -> suppress (still recorded).
        if in_maintenance(self.a.maintenance_windows, datetime.fromtimestamp(now)):
            self.store.add_alert(db, event_type, severity, title, text,
                                 status="suppressed_maintenance", dedup_key=dedup_key,
                                 at_epoch=now)
            return []

        # 2) dedup -> suppress a repeat within the window.
        if self.a.dedup_minutes > 0:
            prior = self.store.recent_alert_since(dedup_key, now - self.a.dedup_minutes * 60)
            if prior:
                self.store.add_alert(db, event_type, severity, title, text,
                                     status="suppressed_dedup", dedup_key=dedup_key,
                                     at_epoch=now)
                return []

        # 3) route by severity + dispatch.
        channels = self.a.channels_for(severity, self._configured_channels())
        results = self._dispatch(channels, event_type, severity, title, text, dedup_key, db)
        ok = [ch for ch, good, _ in results if good]
        escalate_at = (now + self.a.escalation_after_minutes * 60
                       if self.a.escalates(severity) else None)
        self.store.add_alert(db, event_type, severity, title, text, status="fired",
                             channels=",".join(ok), dedup_key=dedup_key,
                             escalate_at=escalate_at, at_epoch=now)
        return results

    # --- escalation --------------------------------------------------------

    def run_escalations(self, log=print) -> int:
        """Escalate any fired alert whose escalate_at has elapsed to the tier-2
        channels. Returns how many were escalated."""
        now = self._now()
        pending = self.store.pending_escalations(now)
        configured = self._configured_channels()
        tier2 = [c for c in self.a.escalation_channels if c in configured]
        n = 0
        for al in pending:
            dedup_key = al.get("dedup_key") or f"{al.get('db_name')}|{al['type']}"
            title = f"[ESCALATED] {al['summary']}"
            # _dispatch returns per-channel (channel, ok, error). Discarding it
            # made an escalation that reached NOBODY -- every tier-2 channel
            # failing, or escalation_channels naming a channel that is not
            # configured, leaving tier2 empty -- indistinguishable from one that
            # paged the on-call: same return count, same "escalated" status,
            # same log line. The alert was then marked escalated, so it dropped
            # out of pending_escalations and never escalated again. Escalation
            # exists precisely for the alert nobody acknowledged, so a silent
            # failure there is the worst place for this to happen.
            results = self._dispatch(tier2, al["type"], al["severity"], title,
                                     al.get("detail") or "", dedup_key,
                                     al.get("db_name"))
            delivered = [ch for ch, good, _ in results if good]
            failed = [(ch, err) for ch, good, err in results if not good]

            if delivered:
                self.store.mark_alert(al["id"], "escalated")
                n += 1
            else:
                # Distinct, queryable status -- never the success status.
                self.store.mark_alert(al["id"], "escalation_failed")

            if failed or not tier2:
                reason = ("no tier-2 channel is configured "
                          f"(escalation.channels={self.a.escalation_channels})"
                          if not tier2 else
                          "; ".join(f"{ch}: {err}" for ch, err in failed))
                self.store.add_event(
                    al.get("db_name"), "error",
                    f"Escalation of alert #{al['id']} ({al['type']}) did not "
                    f"reach {'any' if not delivered else 'every'} tier-2 "
                    f"channel: {reason}")
                # ...and PUSH it. An "error" event is not in EVENT_TYPES, so
                # recording the failure there alone reaches nobody who is not
                # already looking at the dashboard -- which leaves the operator
                # whose escalation path is wired to nothing exactly as uninformed
                # as before. Dispatched directly rather than through notify() so
                # it cannot be deduped or maintenance-suppressed into silence:
                # this is the one message that must not be swallowed. It cannot
                # storm, because mark_alert clears escalate_at, so an alert
                # leaves pending_escalations for good and produces at most one
                # of these, ever. Tier-2 channels that just failed are excluded;
                # re-sending to a dead channel is how the report gets lost.
                fallback = [c for c in self.a.channels_for(
                    "critical", self._configured_channels())
                    if c not in {ch for ch, _ in failed}]
                if fallback:
                    self._dispatch(
                        fallback, "escalation_failed", "critical",
                        f"ESCALATION FAILED for alert #{al['id']} ({al['type']})",
                        f"{al.get('summary') or ''}\n\nEscalation did not reach "
                        f"{'any' if not delivered else 'every'} tier-2 channel: "
                        f"{reason}\n\nThis alert will NOT escalate again.",
                        f"escalation_failed|{dedup_key}", al.get("db_name"))

            if delivered:
                log(f"escalated alert #{al['id']} ({al['type']}) to "
                    f"{', '.join(delivered)}"
                    + (f" (FAILED: {', '.join(ch for ch, _ in failed)})"
                       if failed else ""))
            else:
                log(f"ESCALATION FAILED for alert #{al['id']} ({al['type']}): "
                    + ("no tier-2 channel is configured"
                       if not tier2 else
                       f"every tier-2 channel failed ({', '.join(ch for ch, _ in failed)})"))
        return n
