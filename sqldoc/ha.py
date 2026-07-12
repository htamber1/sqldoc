"""High-availability / replication monitoring across dialects.

Reports replica roles, synchronization/sync state, and lag for each engine's
HA mechanism:

* **SQL Server** — Always On availability groups: ``sys.availability_groups`` +
  ``sys.availability_replicas`` + ``sys.dm_hadr_availability_replica_states`` +
  ``sys.dm_hadr_database_replica_states`` (send/redo queue as the lag proxy).
* **PostgreSQL** — streaming replication: ``pg_stat_replication`` on the primary
  (connected standbys, ``state``, ``sync_state``, replay lag) and
  ``pg_stat_wal_receiver`` on a standby (its upstream).
* **MySQL** — ``SHOW REPLICA STATUS`` (Replica_IO_Running / Replica_SQL_Running /
  Seconds_Behind_Source).

Reads only replication catalog/DMV metadata — never table row data.
"""
from dataclasses import dataclass, field

from sqldoc.dbutil import cell

HA_DIALECTS = {"sqlserver", "azuresql", "azure_managed_instance", "postgres", "mysql"}


def _s(v) -> str:
    return "" if v is None else str(v)


def _i(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _f(v):
    try:
        return None if v is None or v == "" else round(float(v), 1)
    except (TypeError, ValueError):
        return None


@dataclass
class Replica:
    server: str = ""
    role: str = ""              # PRIMARY / SECONDARY / SOURCE / REPLICA
    ag_name: str = ""
    state: str = ""            # operational / connected state
    sync_state: str = ""       # SYNCHRONIZED / SYNCHRONIZING / streaming / ...
    sync_health: str = ""      # HEALTHY / NOT_HEALTHY (SQL Server)
    lag_seconds: float = None
    lag_bytes: int = None
    io_running: str = ""       # MySQL
    sql_running: str = ""      # MySQL

    @property
    def is_secondary(self) -> bool:
        return self.role.upper() in ("SECONDARY", "REPLICA", "STANDBY")

    @property
    def is_healthy(self) -> bool:
        # SQL Server: synchronization_health_desc.
        if self.sync_health and self.sync_health.upper() not in ("HEALTHY", ""):
            return False
        # MySQL: both threads must be running.
        if self.io_running and self.io_running.lower() != "yes":
            return False
        if self.sql_running and self.sql_running.lower() != "yes":
            return False
        # For a secondary/standby, the connection/streaming state must be live.
        # (PostgreSQL sync_state is sync/async — a replication *mode*, not health;
        # the health signal is the streaming `state`.)
        if self.role.upper() in ("REPLICA", "STANDBY", "SECONDARY") and self.state:
            if self.state.lower() not in ("streaming", "running", "connected", "online",
                                          "synchronized", "catch_up", "seeding"):
                return False
        return True


@dataclass
class HaReport:
    dialect: str = ""
    supported: bool = True
    ha_enabled: bool = False
    mechanism: str = ""
    replicas: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# --- SQL Server ------------------------------------------------------------

def _collect_sqlserver(cursor) -> HaReport:
    report = HaReport(dialect="sqlserver", mechanism="Always On availability groups")
    cursor.execute("""
        SELECT ag.name AS ag_name,
               ar.replica_server_name,
               ars.role_desc,
               ars.operational_state_desc,
               ars.synchronization_health_desc,
               ars.connected_state_desc,
               SUM(ISNULL(drs.log_send_queue_size, 0)) AS log_send_queue_kb,
               SUM(ISNULL(drs.redo_queue_size, 0)) AS redo_queue_kb,
               MAX(drs.synchronization_state_desc) AS sync_state
        FROM sys.availability_groups ag
        INNER JOIN sys.availability_replicas ar ON ag.group_id = ar.group_id
        INNER JOIN sys.dm_hadr_availability_replica_states ars ON ar.replica_id = ars.replica_id
        LEFT JOIN sys.dm_hadr_database_replica_states drs ON ars.replica_id = drs.replica_id
        GROUP BY ag.name, ar.replica_server_name, ars.role_desc,
                 ars.operational_state_desc, ars.synchronization_health_desc, ars.connected_state_desc
        ORDER BY ag.name, ars.role_desc
    """)
    for r in cursor.fetchall():
        send_kb = _i(cell(r, "log_send_queue_kb")) or 0
        redo_kb = _i(cell(r, "redo_queue_kb")) or 0
        report.replicas.append(Replica(
            server=_s(cell(r, "replica_server_name")),
            role=_s(cell(r, "role_desc")),
            ag_name=_s(cell(r, "ag_name")),
            state=_s(cell(r, "operational_state_desc")) or _s(cell(r, "connected_state_desc")),
            sync_state=_s(cell(r, "sync_state")),
            sync_health=_s(cell(r, "synchronization_health_desc")),
            lag_bytes=(send_kb + redo_kb) * 1024,
        ))
    report.ha_enabled = bool(report.replicas)
    if not report.ha_enabled:
        report.notes.append("No Always On availability groups are configured on this instance.")
    return report


# --- PostgreSQL ------------------------------------------------------------

def _collect_postgres(cursor) -> HaReport:
    report = HaReport(dialect="postgres", mechanism="streaming replication")
    # On the primary: connected standbys.
    cursor.execute("""
        SELECT application_name, client_addr, state, sync_state,
               EXTRACT(EPOCH FROM replay_lag) AS replay_lag_seconds,
               pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
        FROM pg_stat_replication
    """)
    for r in cursor.fetchall():
        report.replicas.append(Replica(
            server=_s(cell(r, "application_name")) or _s(cell(r, "client_addr")),
            role="REPLICA",
            state=_s(cell(r, "state")),
            sync_state=_s(cell(r, "sync_state")),
            lag_seconds=_f(cell(r, "replay_lag_seconds")),
            lag_bytes=_i(cell(r, "lag_bytes")),
        ))

    # On a standby: its upstream primary (pg_stat_wal_receiver).
    try:
        cursor.execute("SELECT status, sender_host, sender_port FROM pg_stat_wal_receiver")
        for r in cursor.fetchall():
            report.replicas.append(Replica(
                server=_s(cell(r, "sender_host")) + ":" + _s(cell(r, "sender_port")),
                role="UPSTREAM",
                state=_s(cell(r, "status")),
                sync_state=_s(cell(r, "status")),
            ))
    except Exception as e:
        report.errors.append(("pg_stat_wal_receiver", f"{type(e).__name__}: {e}"))

    report.ha_enabled = bool(report.replicas)
    if not report.ha_enabled:
        report.notes.append("No streaming replication detected (no standbys connected and not a standby).")
    return report


# --- MySQL -----------------------------------------------------------------

def _collect_mysql(cursor) -> HaReport:
    report = HaReport(dialect="mysql", mechanism="replication")
    try:
        cursor.execute("SHOW REPLICA STATUS")
    except Exception:
        # Older servers only support SHOW SLAVE STATUS.
        cursor.execute("SHOW SLAVE STATUS")
    for r in cursor.fetchall():
        io = _s(cell(r, "Replica_IO_Running") if _has(r, "Replica_IO_Running") else cell(r, "Slave_IO_Running"))
        sql = _s(cell(r, "Replica_SQL_Running") if _has(r, "Replica_SQL_Running") else cell(r, "Slave_SQL_Running"))
        behind = (cell(r, "Seconds_Behind_Source") if _has(r, "Seconds_Behind_Source")
                  else cell(r, "Seconds_Behind_Master"))
        host = (cell(r, "Source_Host") if _has(r, "Source_Host") else cell(r, "Master_Host"))
        report.replicas.append(Replica(
            server=_s(host), role="REPLICA", io_running=io, sql_running=sql,
            state=("running" if io.lower() == "yes" and sql.lower() == "yes" else "stopped"),
            lag_seconds=_f(behind),
        ))
    report.ha_enabled = bool(report.replicas)
    if not report.ha_enabled:
        report.notes.append("This server is not configured as a replica (SHOW REPLICA STATUS is empty).")
    return report


def _has(row, name) -> bool:
    try:
        cell(row, name)
        return True
    except Exception:
        return False


# --- dispatch --------------------------------------------------------------

def collect_ha(adapter) -> HaReport:
    dialect = getattr(adapter, "dialect", "sqlserver")
    if dialect not in HA_DIALECTS:
        return HaReport(dialect=dialect, supported=False,
                        errors=[("Unsupported", f"HA monitoring is not implemented for {dialect}.")])
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        if dialect == "azure_managed_instance":
            return _collect_azure_geo(cursor)
        if dialect in ("sqlserver", "azuresql"):
            return _collect_sqlserver(cursor)
        if dialect == "postgres":
            return _collect_postgres(cursor)
        return _collect_mysql(cursor)
    finally:
        conn.close()


def _collect_azure_geo(cursor) -> HaReport:
    """Azure SQL Managed Instance HA is built in; show geo-replication links
    (auto-failover groups / geo-replicas) instead of Always On."""
    report = HaReport(dialect="azure_managed_instance", mechanism="Azure geo-replication")
    cursor.execute("""
        SELECT partner_server, partner_database, role_desc,
               replication_state_desc, secondary_allow_connections_desc,
               last_replication, replication_lag_sec
        FROM sys.dm_geo_replication_link_status
    """)
    for r in cursor.fetchall():
        report.replicas.append(Replica(
            server=_s(cell(r, "partner_server")),
            role=_s(cell(r, "role_desc")).upper() or "GEO-SECONDARY",
            state=_s(cell(r, "replication_state_desc")),
            sync_state=_s(cell(r, "replication_state_desc")),
            lag_seconds=_f(cell(r, "replication_lag_sec")),
        ))
    report.ha_enabled = bool(report.replicas)
    if not report.ha_enabled:
        report.notes.append("No geo-replication links configured (single instance). "
                            "Azure still provides built-in local HA.")
    else:
        report.notes.append("Azure-managed geo-replication. Local HA is built in and always on.")
    return report


def behind_replicas(report: HaReport, threshold_seconds: float) -> list:
    """Replicas that are unhealthy or lagging more than `threshold_seconds`
    (used by the agent alert)."""
    out = []
    for r in report.replicas:
        if not r.is_healthy:
            out.append(r)
        elif r.lag_seconds is not None and r.lag_seconds > threshold_seconds:
            out.append(r)
    return out


def summarize(report: HaReport) -> dict:
    reps = report.replicas
    lags = [r.lag_seconds for r in reps if r.lag_seconds is not None]
    return {
        "ha_enabled": report.ha_enabled,
        "mechanism": report.mechanism,
        "replicas": len(reps),
        "unhealthy": sum(1 for r in reps if not r.is_healthy),
        "secondaries": sum(1 for r in reps if r.is_secondary),
        "max_lag_seconds": max(lags) if lags else None,
    }
