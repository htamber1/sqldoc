"""Amazon Aurora adapters (Aurora PostgreSQL + Aurora MySQL).

Aurora is wire-compatible with PostgreSQL / MySQL, so metadata extraction is
inherited unchanged. These thin subclasses add Aurora-specific replication
metrics:

* **Aurora MySQL** — per-instance replica lag from
  ``information_schema.replica_host_status`` (``REPLICA_LAG_IN_MILLISECONDS``,
  CPU), with writer/reader roles.
* **Aurora PostgreSQL** — replica lag from ``aurora_replica_status()``.

Serverless-v2 ACU and global-database lag are CloudWatch metrics (not exposed
via SQL), so they are noted but not queried. Detected from a
``*.rds.amazonaws.com`` host whose connection string contains ``aurora``;
PostgreSQL vs MySQL is chosen by scheme/port.

NOTE: mock-tested only — not run against a live Aurora cluster.
"""
from sqldoc.adapters.postgres import PostgresAdapter
from sqldoc.adapters.mysql import MySQLAdapter
from sqldoc.dbutil import cell


def _s(v):
    return "" if v is None else str(v)


def _num(v):
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


class AuroraMySQLAdapter(MySQLAdapter):
    dialect = "aurora_mysql"
    display_name = "Amazon Aurora MySQL"

    def aurora_replica_lag(self) -> list:
        """Per-instance replica lag from information_schema.replica_host_status."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = []
        try:
            cursor.execute("""
                SELECT SERVER_ID, SESSION_ID,
                       REPLICA_LAG_IN_MILLISECONDS AS lag_ms, CPU
                FROM information_schema.replica_host_status
            """)
            for r in cursor.fetchall():
                is_writer = _s(cell(r, "SESSION_ID")) == "MASTER_SESSION_ID"
                out.append({"server_id": _s(cell(r, "SERVER_ID")),
                            "role": "WRITER" if is_writer else "READER",
                            "lag_ms": (0.0 if is_writer else _num(cell(r, "lag_ms"))),
                            "cpu": _num(cell(r, "CPU"))})
        except Exception:
            pass
        finally:
            conn.close()
        return out


class AuroraPostgresAdapter(PostgresAdapter):
    dialect = "aurora_postgres"
    display_name = "Amazon Aurora PostgreSQL"

    def aurora_replica_lag(self) -> list:
        """Replica lag from the aurora_replica_status() function."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = []
        try:
            cursor.execute("""
                SELECT server_id, replica_lag_in_msec AS lag_ms, cpu, is_current
                FROM aurora_replica_status()
            """)
            for r in cursor.fetchall():
                out.append({"server_id": _s(cell(r, "server_id")),
                            "role": "READER",
                            "lag_ms": _num(cell(r, "lag_ms")),
                            "cpu": _num(cell(r, "cpu")),
                            "is_current": bool(cell(r, "is_current"))})
        except Exception:
            pass
        finally:
            conn.close()
        return out
