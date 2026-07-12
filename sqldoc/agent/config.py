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
               "backup_stale", "replica_lag"]


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
    smtp: dict = None
    on: list = field(default_factory=lambda: list(EVENT_TYPES))


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
    # Backup monitoring (all dialects with a backup/PITR mechanism).
    backup_monitoring: bool = False
    backup_max_age_hours: float = 24.0       # alert when a database's last backup is older
    # HA / replication monitoring (all dialects with a replication mechanism).
    ha_monitoring: bool = False
    replica_lag_threshold_seconds: float = 30.0  # alert when a replica lags more than this


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
    backup_monitoring = bool(agent.get("backup_monitoring", False))
    backup_max_age_hours = float(agent.get("backup_max_age_hours", 24.0))
    ha_monitoring = bool(agent.get("ha_monitoring", False))
    replica_lag_threshold = float(agent.get("replica_lag_threshold_seconds", 30.0))

    raw_dbs = agent.get("databases") or []
    if not isinstance(raw_dbs, list) or not raw_dbs:
        raise ValueError("agent.databases must be a non-empty list of database entries.")

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
        smtp=n.get("email"),
        on=list(on),
    )

    return AgentConfig(
        interval_minutes=interval, dashboard_port=port, mode=mode, model=model,
        no_ai=no_ai, concurrency=concurrency, databases=databases, notify=notify,
        server_monitoring=server_monitoring, disk_threshold_percent=disk_threshold,
        errorlog_severity=errorlog_severity,
        backup_monitoring=backup_monitoring, backup_max_age_hours=backup_max_age_hours,
        ha_monitoring=ha_monitoring, replica_lag_threshold_seconds=replica_lag_threshold,
    )
