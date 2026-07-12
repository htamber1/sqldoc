"""Azure Synapse Analytics (dedicated SQL pool) adapter.

Synapse dedicated pools speak T-SQL over pyodbc, so tables/views/procedures come
from the same ``sys.*`` catalog as SQL Server (inherited). What is unique to
Synapse is the **distribution model**, which this adapter surfaces:

* Each table's **distribution type** (HASH / ROUND_ROBIN / REPLICATE) and, for
  HASH, its **distribution column** — from ``sys.pdw_table_distribution_properties``
  + ``sys.pdw_column_distribution_properties``.
* **Data skew** — how unevenly rows are spread across the 60 distributions
  (``sys.dm_pdw_nodes_db_partition_stats``); high skew hurts query parallelism.
* **Workload management groups** — importance + concurrency slots
  (``sys.workload_management_workload_groups``).

The distribution type + column + skew are folded into each table's description so
they appear in the standard `sqldoc doc` output. Detected from a
``*.sql.azuresynapse.net`` host.

NOTE: mock-tested only — not run against a live Synapse workspace.
"""
from sqldoc.adapters.base import Capabilities
from sqldoc.adapters.sqlserver import SqlServerAdapter


class SynapseAdapter(SqlServerAdapter):
    dialect = "synapse"
    display_name = "Azure Synapse Analytics"
    # Metadata + distribution model; the DMV-based health/quality/server checks
    # differ on the MPP engine and are not ported.
    capabilities = Capabilities(quality=False, health=False, access_audit=False,
                                triggers=False)

    # --- distribution model ------------------------------------------------

    def synapse_distribution(self) -> dict:
        """Return {(schema, table): (distribution, dist_column, skew_pct)}."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = {}
        try:
            cursor.execute("""
                SELECT s.name AS schema_name, t.name AS table_name,
                       tp.distribution_policy_desc AS distribution,
                       c.name AS dist_column,
                       skew.skew_pct AS skew_pct
                FROM sys.tables t
                INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
                INNER JOIN sys.pdw_table_distribution_properties tp ON t.object_id = tp.object_id
                LEFT JOIN sys.pdw_column_distribution_properties cp
                    ON t.object_id = cp.object_id AND cp.distribution_ordinal = 1
                LEFT JOIN sys.columns c
                    ON cp.object_id = c.object_id AND cp.column_id = c.column_id
                OUTER APPLY (
                    SELECT CASE WHEN AVG(nps.row_count * 1.0) = 0 THEN 0
                                ELSE (MAX(nps.row_count) - MIN(nps.row_count)) * 100.0
                                     / AVG(nps.row_count * 1.0) END AS skew_pct
                    FROM sys.dm_pdw_nodes_db_partition_stats nps
                    WHERE nps.object_id = t.object_id
                ) AS skew
            """)
            for r in cursor.fetchall():
                key = (self._val(r, "schema_name"), self._val(r, "table_name"))
                out[key] = (self._val(r, "distribution"), self._val(r, "dist_column"),
                            self._num(r, "skew_pct"))
        except Exception:
            pass
        finally:
            conn.close()
        return out

    def synapse_workload_groups(self) -> list:
        """Workload-management groups with concurrency slots."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = []
        try:
            cursor.execute("""
                SELECT name AS group_name,
                       importance,
                       min_percentage_resource,
                       cap_percentage_resource,
                       request_min_resource_grant_percent,
                       query_execution_timeout_sec
                FROM sys.workload_management_workload_groups
                ORDER BY name
            """)
            for r in cursor.fetchall():
                grant = self._num(r, "request_min_resource_grant_percent") or 0
                slots = int(100 / grant) if grant else None
                out.append({
                    "group": self._val(r, "group_name"),
                    "importance": self._val(r, "importance"),
                    "min_pct": self._num(r, "min_percentage_resource"),
                    "cap_pct": self._num(r, "cap_percentage_resource"),
                    "min_grant_pct": grant,
                    "concurrency_slots": slots,
                    "timeout_sec": self._num(r, "query_execution_timeout_sec"),
                })
        except Exception:
            pass
        finally:
            conn.close()
        return out

    # --- metadata (inherited + distribution enrichment) --------------------

    def extract_metadata(self):
        tables = super().extract_metadata()
        dist = self.synapse_distribution()
        for t in tables:
            info = dist.get((t.schema, t.name))
            if not info:
                continue
            dtype, dcol, skew = info
            tag = f"[Distribution: {dtype}"
            if dtype and "HASH" in str(dtype).upper() and dcol:
                tag += f" on {dcol}"
            if skew is not None and skew >= 1:
                tag += f", skew {round(skew, 1)}%"
            tag += "]"
            t.description = (tag + " " + (t.description or "")).strip()
        return tables

    # --- helpers (tolerate dict rows and pyodbc rows) ----------------------

    @staticmethod
    def _val(row, name):
        try:
            v = row[name]
        except (KeyError, IndexError, TypeError):
            v = getattr(row, name, None)
        return None if v is None else str(v)

    @staticmethod
    def _num(row, name):
        try:
            v = row[name]
        except (KeyError, IndexError, TypeError):
            v = getattr(row, name, None)
        try:
            return None if v is None else round(float(v), 2)
        except (TypeError, ValueError):
            return None
