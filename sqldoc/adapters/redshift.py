"""Amazon Redshift adapter.

Redshift speaks the PostgreSQL wire protocol, so tables/views/procedures come
from the inherited :class:`PostgresAdapter` extraction (``information_schema`` +
``pg_catalog``, which Redshift supports). What is unique to Redshift is its MPP
storage model, surfaced here:

* Per-table **distribution style** (EVEN / KEY(col) / ALL) and **sort key**,
  plus **skew** and **unsorted-rows %** — from ``svv_table_info`` (folded into
  the table description so it shows in `sqldoc doc`).
* **WLM queue** configuration (concurrency slots) — ``stv_wlm_service_class_config``.
* **VACUUM / ANALYZE recommendations** — parsed from ``stl_alert_event_log``.

Uses the same ``psycopg2`` driver as PostgreSQL. Detected from a
``*.redshift.amazonaws.com`` host or a ``redshift://`` scheme.

NOTE: mock-tested only — not run against a live Redshift cluster.
"""
from sqldoc.adapters.base import Capabilities
from sqldoc.adapters.postgres import PostgresAdapter
from sqldoc.dbutil import cell


def _s(v):
    return "" if v is None else str(v)


def _num(v):
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


class RedshiftAdapter(PostgresAdapter):
    dialect = "redshift"
    display_name = "Amazon Redshift"
    # Metadata + distribution model; PG's pg_stat_* health/quality SQL does not
    # apply to Redshift's MPP engine.
    capabilities = Capabilities(quality=False, health=False, access_audit=False)

    # --- Redshift-specific metadata ----------------------------------------

    def redshift_table_info(self) -> dict:
        """Return {(schema, table): {diststyle, sortkey, skew, unsorted, rows}}."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = {}
        try:
            cursor.execute("""
                SELECT "schema" AS schema_name, "table" AS table_name,
                       diststyle, sortkey1, skew_rows, unsorted, tbl_rows
                FROM svv_table_info
            """)
            for r in cursor.fetchall():
                out[(_s(cell(r, "schema_name")), _s(cell(r, "table_name")))] = {
                    "diststyle": _s(cell(r, "diststyle")),
                    "sortkey": _s(cell(r, "sortkey1")),
                    "skew": _num(cell(r, "skew_rows")),
                    "unsorted": _num(cell(r, "unsorted")),
                    "rows": _num(cell(r, "tbl_rows")),
                }
        except Exception:
            pass
        finally:
            conn.close()
        return out

    def redshift_wlm_queues(self) -> list:
        conn = self.connect()
        cursor = self.cursor(conn)
        out = []
        try:
            cursor.execute("""
                SELECT service_class, num_query_tasks AS slots, query_working_mem
                FROM stv_wlm_service_class_config
                WHERE service_class > 4
                ORDER BY service_class
            """)
            for r in cursor.fetchall():
                out.append({
                    "service_class": int(cell(r, "service_class") or 0),
                    "concurrency_slots": int(cell(r, "slots") or 0),
                    "working_mem_mb": _num(cell(r, "query_working_mem")),
                })
        except Exception:
            pass
        finally:
            conn.close()
        return out

    def redshift_recommendations(self) -> list:
        """VACUUM / ANALYZE (and other) recommendations from stl_alert_event_log."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = []
        try:
            cursor.execute("""
                SELECT TRIM(event) AS event, TRIM(solution) AS solution, COUNT(*) AS occurrences
                FROM stl_alert_event_log
                GROUP BY TRIM(event), TRIM(solution)
                ORDER BY COUNT(*) DESC
                LIMIT 20
            """)
            for r in cursor.fetchall():
                out.append({
                    "event": _s(cell(r, "event")),
                    "solution": _s(cell(r, "solution")),
                    "occurrences": int(cell(r, "occurrences") or 0),
                })
        except Exception:
            pass
        finally:
            conn.close()
        return out

    # --- enrichment --------------------------------------------------------

    @staticmethod
    def _enrich(tables, info):
        for t in tables:
            data = info.get((t.schema, t.name))
            if not data:
                continue
            parts = [f"DISTSTYLE {data['diststyle']}"] if data.get("diststyle") else []
            if data.get("sortkey"):
                parts.append(f"SORTKEY {data['sortkey']}")
            if data.get("skew") and data["skew"] > 1:
                parts.append(f"skew {data['skew']}")
            if data.get("unsorted") and data["unsorted"] >= 1:
                parts.append(f"unsorted {data['unsorted']}%")
            if parts:
                tag = "[Redshift: " + ", ".join(parts) + "]"
                t.description = (tag + " " + (t.description or "")).strip()
        return tables

    def extract_metadata(self):
        tables = super().extract_metadata()
        return self._enrich(tables, self.redshift_table_info())
