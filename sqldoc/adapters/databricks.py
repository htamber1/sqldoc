"""Databricks (SQL warehouse / Unity Catalog) adapter.

Connects to a Databricks SQL warehouse via ``databricks-sql-connector`` and reads
Delta-table metadata from Unity Catalog's ``information_schema``:

* Tables + columns, with **partition columns** (``information_schema.columns.
  partition_index``) folded into the table description.
* Informational primary keys (``information_schema`` constraints).
* Per-table **Delta details** — version-history length (``DESCRIBE HISTORY``),
  file count + size (``DESCRIBE DETAIL``) — and OPTIMIZE / VACUUM
  recommendations derived from small-file counts.

The driver is an *optional* dependency imported lazily; a missing driver raises a
clear ``pip install sqldoc[databricks]`` error. Detected from a
``*.azuredatabricks.net`` or ``*.databricks.com`` host, or a ``databricks://``
scheme.

NOTE: mock-tested only — not run against a live Databricks workspace.
"""
from urllib.parse import parse_qs, unquote, urlparse

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities, Table, Column, View, Parameter, StoredProcedure,
)
from sqldoc.dbutil import cell


def _s(v):
    return "" if v is None else str(v)


class DatabricksAdapter(DatabaseAdapter):
    dialect = "databricks"
    display_name = "Databricks"
    capabilities = Capabilities(quality=False, health=False, access_audit=False,
                                triggers=False, computed_columns=False)

    @staticmethod
    def _default_connect(connection_string: str):
        try:
            from databricks import sql as dbsql
        except ImportError as e:
            raise ImportError(
                "Databricks support requires the 'databricks-sql-connector' driver, "
                "which is not installed. Install it with:  pip install sqldoc[databricks]  "
                "(or:  pip install databricks-sql-connector)."
            ) from e
        u = urlparse(connection_string)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        # databricks://token:<access_token>@<host>/<http_path>?catalog=..&schema=..
        http_path = (u.path or "").lstrip("/")
        token = unquote(u.password) if u.password else q.get("token")
        return dbsql.connect(
            server_hostname=u.hostname,
            http_path=http_path or q.get("http_path"),
            access_token=token,
            catalog=q.get("catalog"),
            schema=q.get("schema"),
        )

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        # server = hostname/http_path ; password = access token ; database = catalog
        host, _, path = server.partition("/")
        return f"databricks://token:{password}@{host}/{path}?catalog={database}"

    # --- tables ------------------------------------------------------------

    def extract_metadata(self) -> list[Table]:
        conn = self.connect()
        cursor = self.cursor(conn)
        cursor.execute("""
            SELECT table_schema AS schema_name, table_name, comment  -- DBX_TABLES
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema')
            ORDER BY table_schema, table_name
        """)
        tables_raw = cursor.fetchall()
        pk_by_table = self._primary_keys(cursor)

        tables = []
        for row in tables_raw:
            schema_name = _s(cell(row, "schema_name"))
            table_name = _s(cell(row, "table_name"))
            pk_cols = pk_by_table.get((schema_name, table_name), set())
            columns, partition_cols = self._columns(cursor, schema_name, table_name, pk_cols)
            desc = _s(cell(row, "comment")) or None
            if partition_cols:
                tag = f"[Delta: partitioned by {', '.join(partition_cols)}]"
                desc = (tag + " " + (desc or "")).strip()
            tables.append(Table(
                schema=schema_name, name=table_name, row_count=0,
                columns=columns, indexes=[], triggers=[],
                check_constraints=[], unique_constraints=[], description=desc,
            ))
        conn.close()
        return tables

    def _columns(self, cursor, schema_name, table_name, pk_cols):
        cursor.execute("""
            SELECT column_name, full_data_type AS data_type, is_nullable,
                   comment, partition_index  -- DBX_COLUMNS
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """, (schema_name, table_name))
        columns, partition_cols = [], []
        for r in cursor.fetchall():
            name = _s(cell(r, "column_name"))
            if cell(r, "partition_index") is not None:
                partition_cols.append(name)
            columns.append(Column(
                name=name, data_type=_s(cell(r, "data_type")), max_length=None,
                is_nullable=(_s(cell(r, "is_nullable")).upper() == "YES"),
                is_primary_key=name in pk_cols, is_foreign_key=False,
                references_table=None, references_column=None,
                description=(_s(cell(r, "comment")) or None),
            ))
        return columns, partition_cols

    def _primary_keys(self, cursor) -> dict:
        out = {}
        cursor.execute("""
            SELECT kcu.table_schema AS schema_name, kcu.table_name, kcu.column_name  -- DBX_PK
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY'
        """)
        for r in cursor.fetchall():
            out.setdefault((_s(cell(r, "schema_name")), _s(cell(r, "table_name"))), set()).add(
                _s(cell(r, "column_name")))
        return out

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        conn = self.connect()
        cursor = self.cursor(conn)
        cursor.execute("""
            SELECT table_schema AS schema_name, table_name AS view_name,
                   view_definition AS definition  -- DBX_VIEWS
            FROM information_schema.views
            WHERE table_schema NOT IN ('information_schema')
            ORDER BY table_schema, table_name
        """)
        views = []
        for row in cursor.fetchall():
            views.append(View(
                schema=_s(cell(row, "schema_name")), name=_s(cell(row, "view_name")),
                columns=[], definition=(_s(cell(row, "definition")) or None)))
        conn.close()
        return views

    # --- procedures / functions --------------------------------------------

    def extract_procedures(self) -> list[StoredProcedure]:
        conn = self.connect()
        cursor = self.cursor(conn)
        cursor.execute("""
            SELECT routine_schema AS schema_name, routine_name AS proc_name,
                   routine_definition AS definition  -- DBX_ROUTINES
            FROM information_schema.routines
            WHERE routine_schema NOT IN ('information_schema')
            ORDER BY routine_schema, routine_name
        """)
        procs = []
        for r in cursor.fetchall():
            procs.append(StoredProcedure(
                schema=_s(cell(r, "schema_name")), name=_s(cell(r, "proc_name")),
                parameters=[], definition=(_s(cell(r, "definition")) or None)))
        conn.close()
        return procs

    # --- Delta details + recommendations -----------------------------------

    def delta_details(self, schema: str, table: str) -> dict:
        """Version-history length + file count/size + an OPTIMIZE/VACUUM hint."""
        conn = self.connect()
        cursor = self.cursor(conn)
        out = {"table": f"{schema}.{table}", "version_count": 0, "num_files": 0,
               "size_mb": 0.0, "recommendation": ""}
        try:
            cursor.execute(f"DESCRIBE HISTORY `{schema}`.`{table}`  -- DBX_HISTORY")
            out["version_count"] = len(cursor.fetchall())
            cursor.execute(f"DESCRIBE DETAIL `{schema}`.`{table}`  -- DBX_DETAIL")
            rows = cursor.fetchall()
            if rows:
                r = rows[0]
                out["num_files"] = int(cell(r, "numFiles") or 0)
                out["size_mb"] = round(float(cell(r, "sizeInBytes") or 0) / 1048576.0, 1)
                if out["num_files"] > 1000:
                    out["recommendation"] = "Many small files — run OPTIMIZE (and VACUUM to reclaim space)."
        except Exception:
            pass
        finally:
            conn.close()
        return out
