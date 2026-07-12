"""CockroachDB adapter.

CockroachDB is PostgreSQL wire-compatible, so tables/views/procedures come from
the inherited :class:`PostgresAdapter` extraction (``information_schema`` +
``pg_catalog``, which CRDB implements). This adapter adds the CRDB-specific
distributed-SQL metadata:

* **Zone configurations** — replication/placement rules per object
  (``crdb_internal.zones``).
* **Node locality / regions** — where the cluster's nodes live
  (``crdb_internal.gossip_nodes``).

Uses the same ``psycopg2`` driver as PostgreSQL. Detected from a
``*.cockroachlabs.cloud`` host or a ``cockroachdb://`` scheme.

NOTE: mock-tested only — not run against a live CockroachDB cluster.
"""
from sqldoc.adapters.base import Capabilities
from sqldoc.adapters.postgres import PostgresAdapter
from sqldoc.dbutil import cell


def _s(v):
    return "" if v is None else str(v)


class CockroachDBAdapter(PostgresAdapter):
    dialect = "cockroachdb"
    display_name = "CockroachDB"
    # PG-compatible metadata; the pg_stat_* health/quality SQL doesn't map to
    # CRDB's distributed engine, so those stay off.
    capabilities = Capabilities(quality=False, health=False, access_audit=False)

    def crdb_zone_configs(self) -> list:
        """Replication / placement zone configs per object."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = []
        try:
            cursor.execute("""
                SELECT target, raw_config_sql
                FROM crdb_internal.zones
                WHERE raw_config_sql IS NOT NULL
                ORDER BY target
            """)
            for r in cursor.fetchall():
                out.append({"target": _s(cell(r, "target")),
                            "config": _s(cell(r, "raw_config_sql"))})
        except Exception:
            pass
        finally:
            conn.close()
        return out

    def crdb_localities(self) -> list:
        """Cluster node localities (region/zone placement)."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = []
        try:
            cursor.execute("SELECT node_id, locality FROM crdb_internal.gossip_nodes ORDER BY node_id")
            for r in cursor.fetchall():
                out.append({"node_id": int(cell(r, "node_id") or 0),
                            "locality": _s(cell(r, "locality"))})
        except Exception:
            pass
        finally:
            conn.close()
        return out
