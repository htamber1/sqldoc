"""Parse the ``agent:`` section of ``.sqldoc.yml`` into typed config.

Example::

    agent:
      interval_minutes: 30
      dashboard_port: 8080
      mode: local          # AI mode: local (Ollama) or cloud (Anthropic)
      model: null
      no_ai: false
      databases:
        - name: prod
          connection_string: "postgresql://user:pw@host/db"
        - name: warehouse
          dialect: sqlserver
          server: localhost
          database: AdventureWorks2022
          username: sa
          password: "***"
      notifications:
        slack_webhook: "https://hooks.slack.com/services/..."
        email:
          smtp_host: smtp.example.com
          smtp_port: 587
          username: alerts@example.com
          password: "***"
          from: sqldoc@example.com
          to: [dba@example.com]
        on: [schema_change, new_pii, health_degradation]
"""
from dataclasses import dataclass, field

from sqldoc.adapters import DIALECTS, detect_dialect

EVENT_TYPES = ["schema_change", "new_pii", "health_degradation",
               "job_failure", "disk_low", "errorlog_critical", "linked_server_down",
               "backup_stale", "replica_lag", "tempdb_version_store", "nl_alert",
               "doc_updated",
               "cms_server_added", "cms_server_removed", "cms_server_unreachable"]


@dataclass
class DatabaseConfig:
    name: str
    connection_string: str
    dialect: str = None
    mode: str = "local"
    model: str = None
    no_ai: bool = False


@dataclass
class NotifyConfig:
    slack_webhook: str = None
    teams_webhook: str = None
    webex: dict = None            # {token, room_id}
    twilio: dict = None           # {account_sid, auth_token, from_number, to}
    whatsapp: dict = None         # {token, phone_number_id, to}
    sms_gateway: dict = None      # an SMTP dict whose `to` are carrier gateway addresses
    smtp: dict = None
    on: list = field(default_factory=lambda: list(EVENT_TYPES))


_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


@dataclass
class WeeklyReportConfig:
    enabled: bool = False
    weekday: int = 0     # 0 = Monday
    hour: int = 8        # local hour (0-23) to send at or after


def _parse_weekly(raw) -> WeeklyReportConfig:
    """Accept `weekly_report: true` or a mapping {enabled, day, hour}."""
    if raw is None or raw is False:
        return WeeklyReportConfig(enabled=False)
    if raw is True:
        return WeeklyReportConfig(enabled=True)
    if not isinstance(raw, dict):
        raise ValueError("agent.weekly_report must be true/false or a mapping.")
    enabled = bool(raw.get("enabled", True))
    day = raw.get("day", "monday")
    if isinstance(day, str):
        wd = _WEEKDAYS.get(day.strip().lower())
        if wd is None:
            raise ValueError(f"agent.weekly_report.day '{day}' is not a weekday name.")
    else:
        wd = int(day)
        if not 0 <= wd <= 6:
            raise ValueError("agent.weekly_report.day as a number must be 0 (Mon) - 6 (Sun).")
    hour = int(raw.get("hour", 8))
    if not 0 <= hour <= 23:
        raise ValueError("agent.weekly_report.hour must be 0-23.")
    return WeeklyReportConfig(enabled=enabled, weekday=wd, hour=hour)


@dataclass
class AgentConfig:
    interval_minutes: int = 30
    dashboard_port: int = 8080
    mode: str = "local"
    model: str = None
    no_ai: bool = False
    concurrency: int = 8
    databases: list = field(default_factory=list)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    # Server-level infrastructure monitoring (SQL Server only).
    server_monitoring: bool = False
    disk_threshold_percent: float = 10.0     # alert when a volume drops below this % free
    errorlog_severity: int = 17              # alert on ERRORLOG entries at/above this severity
    tempdb_version_store_mb: float = 2048.0  # alert when the tempdb version store exceeds this
    # Backup monitoring (all dialects with a backup/PITR mechanism).
    backup_monitoring: bool = False
    backup_max_age_hours: float = 24.0       # alert when a database's last backup is older
    # HA / replication monitoring (all dialects with a replication mechanism).
    ha_monitoring: bool = False
    replica_lag_threshold_seconds: float = 30.0  # alert when a replica lags more than this
    # Natural-language alert rules (plain English; evaluated by the LLM each poll).
    nl_alerts: list = field(default_factory=list)
    # Scheduled weekly email digest (emailed on the configured weekday/hour).
    weekly_report: WeeklyReportConfig = field(default_factory=WeeklyReportConfig)
    # Integration auto-push: names of configured report connectors (sharepoint,
    # confluence, notion, ...) to push docs to on a fixed cadence.
    integrations: list = field(default_factory=list)
    push_interval_hours: float = 24.0
    # Enterprise alert management (parsed AlertingConfig, or None). Read from the
    # top-level `alerting:` section, outside the `agent:` block.
    alerting: object = None
    # CMS monitoring: when set, the agent monitors every registered server and
    # reconciles the CMS registration on a cadence. Config: {server, windows_auth,
    # database, reconcile_minutes, ...}.
    cms: dict = None
    cms_reconcile_minutes: float = 15.0
    # The full .sqldoc.yml mapping, so the push loop can read the top-level
    # integration config sections (they live outside the `agent:` block).
    raw_config: dict = None


def _resolve_connection(entry: dict) -> tuple:
    """Return (connection_string, dialect) for one database entry."""
    dialect = entry.get("dialect")
    cs = entry.get("connection_string")
    if cs:
        return cs, (dialect or detect_dialect(cs))
    server = entry.get("server")
    database = entry.get("database")
    username = entry.get("username")
    password = entry.get("password")
    missing = [k for k, v in (("server", server), ("database", database),
                              ("username", username), ("password", password)) if not v]
    if missing:
        raise ValueError(
            f"database '{entry.get('name', '?')}' needs either a connection_string or "
            f"server/database/username/password (missing: {', '.join(missing)}).")
    dialect = dialect or "sqlserver"
    adapter_cls = DIALECTS.get(dialect)
    if adapter_cls is None:
        raise ValueError(f"database '{entry.get('name', '?')}' has unknown dialect '{dialect}'.")
    cs = adapter_cls.build_connection_string(server, database, username, password)
    return cs, dialect


def parse_agent_config(cfg: dict) -> AgentConfig:
    """Build an AgentConfig from a loaded .sqldoc.yml mapping. Raises ValueError
    with an actionable message on a malformed ``agent:`` section."""
    agent = (cfg or {}).get("agent")
    if not agent:
        raise ValueError(
            "No 'agent:' section in the config. Add one to .sqldoc.yml (see docs) "
            "with at least one database under 'databases:'.")
    if not isinstance(agent, dict):
        raise ValueError("The 'agent:' config must be a mapping.")

    interval = int(agent.get("interval_minutes", 30))
    if interval < 1:
        raise ValueError("agent.interval_minutes must be at least 1.")
    port = int(agent.get("dashboard_port", 8080))
    mode = agent.get("mode", "local")
    model = agent.get("model")
    no_ai = bool(agent.get("no_ai", False))
    concurrency = int(agent.get("concurrency", 8))
    server_monitoring = bool(agent.get("server_monitoring", False))
    disk_threshold = float(agent.get("disk_threshold_percent", 10.0))
    errorlog_severity = int(agent.get("errorlog_severity", 17))
    tempdb_vstore_mb = float(agent.get("tempdb_version_store_mb", 2048.0))
    backup_monitoring = bool(agent.get("backup_monitoring", False))
    backup_max_age_hours = float(agent.get("backup_max_age_hours", 24.0))
    ha_monitoring = bool(agent.get("ha_monitoring", False))
    replica_lag_threshold = float(agent.get("replica_lag_threshold_seconds", 30.0))
    raw_alerts = agent.get("alerts") or []
    if not isinstance(raw_alerts, list) or any(not isinstance(a, str) for a in raw_alerts):
        raise ValueError("agent.alerts must be a list of plain-English rule strings.")
    nl_alerts = [a.strip() for a in raw_alerts if a.strip()]
    weekly_report = _parse_weekly(agent.get("weekly_report"))

    from sqldoc.integrations import _MODULES as _INTEGRATION_MODULES
    raw_integrations = agent.get("integrations") or []
    if not isinstance(raw_integrations, list):
        raise ValueError("agent.integrations must be a list of connector names.")
    bad_int = [i for i in raw_integrations if i not in _INTEGRATION_MODULES]
    if bad_int:
        raise ValueError(
            f"unknown agent.integrations {bad_int}; choose from "
            f"{sorted(_INTEGRATION_MODULES)}.")
    push_interval_hours = float(agent.get("push_interval_hours", 24.0))
    if push_interval_hours < 1:
        raise ValueError("agent.push_interval_hours must be at least 1.")

    from sqldoc.agent.alerting import parse_alerting
    alerting = parse_alerting(cfg)

    cms_cfg = agent.get("cms")
    if cms_cfg is not None and not isinstance(cms_cfg, dict):
        raise ValueError("agent.cms must be a mapping (with at least a 'server').")
    if cms_cfg and not cms_cfg.get("server"):
        raise ValueError("agent.cms needs a 'server' (the CMS instance name).")
    cms_reconcile_minutes = float(agent.get("cms_reconcile_minutes", 15.0))

    raw_dbs = agent.get("databases") or []
    # With CMS monitoring the database list is populated from the CMS at startup,
    # so an explicit databases list is optional.
    if not isinstance(raw_dbs, list) or (not raw_dbs and not cms_cfg):
        raise ValueError("agent.databases must be a non-empty list of database entries "
                         "(or configure agent.cms to monitor a Central Management Server).")

    databases = []
    seen = set()
    for entry in raw_dbs:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ValueError("each agent.databases entry needs a 'name'.")
        name = entry["name"]
        if name in seen:
            raise ValueError(f"duplicate database name '{name}' in agent.databases.")
        seen.add(name)
        cs, dialect = _resolve_connection(entry)
        databases.append(DatabaseConfig(
            name=name, connection_string=cs, dialect=dialect,
            mode=entry.get("mode", mode), model=entry.get("model", model),
            no_ai=bool(entry.get("no_ai", no_ai)),
        ))

    n = agent.get("notifications") or {}
    on = n.get("on") or list(EVENT_TYPES)
    bad = [e for e in on if e not in EVENT_TYPES]
    if bad:
        raise ValueError(
            f"unknown notification event(s) {bad}; choose from {EVENT_TYPES}.")
    notify = NotifyConfig(
        slack_webhook=n.get("slack_webhook"),
        teams_webhook=n.get("teams_webhook"),
        webex=n.get("webex"),
        twilio=n.get("twilio"),
        whatsapp=n.get("whatsapp"),
        sms_gateway=n.get("sms_gateway"),
        smtp=n.get("email"),
        on=list(on),
    )

    return AgentConfig(
        interval_minutes=interval, dashboard_port=port, mode=mode, model=model,
        no_ai=no_ai, concurrency=concurrency, databases=databases, notify=notify,
        server_monitoring=server_monitoring, disk_threshold_percent=disk_threshold,
        errorlog_severity=errorlog_severity, tempdb_version_store_mb=tempdb_vstore_mb,
        backup_monitoring=backup_monitoring, backup_max_age_hours=backup_max_age_hours,
        ha_monitoring=ha_monitoring, replica_lag_threshold_seconds=replica_lag_threshold,
        nl_alerts=nl_alerts, weekly_report=weekly_report,
        integrations=list(raw_integrations), push_interval_hours=push_interval_hours,
        alerting=alerting, cms=cms_cfg, cms_reconcile_minutes=cms_reconcile_minutes,
        raw_config=cfg,
    )
