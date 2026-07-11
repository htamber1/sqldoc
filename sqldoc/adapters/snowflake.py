"""Snowflake adapter.

Uses Snowflake's `INFORMATION_SCHEMA` for tables/columns/views/procedures and
`SHOW PRIMARY KEYS` / `SHOW IMPORTED KEYS` for keys (Snowflake exposes
constraints through SHOW commands rather than a KEY_COLUMN_USAGE view). The
driver (`snowflake-connector-python`) is an *optional* dependency imported
lazily inside `_default_connect`; a missing driver raises a clear error.

Snowflake has no indexes or triggers (it uses micro-partitions), so those are
always empty. CHECK/UNIQUE constraints are not introspected. Identifiers in
Snowflake are upper-cased unless quoted, so every SELECT alias is double-quoted
lower-case to give stable dict keys.

NOTE: this adapter is **mock-tested only** — it has not yet run against a live
Snowflake account (that needs credentials). The SQL follows Snowflake's
documented INFORMATION_SCHEMA / SHOW surface.

Connection string form:
`snowflake://user:password@account/database/schema?warehouse=wh&role=r`
"""
from urllib.parse import parse_qs, unquote, urlparse

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities,
    Table, Column, View, Parameter, StoredProcedure,
)


def _parse_arguments(signature: str) -> list[Parameter]:
    """Parse an ARGUMENT_SIGNATURE like '(A NUMBER, B VARCHAR)' into params."""
    sig = (signature or "").strip()
    if sig.startswith("(") and sig.endswith(")"):
        sig = sig[1:-1]
    params = []
    for part in sig.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(None, 1)
        params.append(Parameter(
            name=bits[0],
            data_type=(bits[1] if len(bits) > 1 else "").strip(),
            max_length=None,
            is_output=False,
        ))
    return params


class SnowflakeAdapter(DatabaseAdapter):
    dialect = "snowflake"
    display_name = "Snowflake"
    # Metadata only for now; health/quality/comply are not ported.
    capabilities = Capabilities(quality=False, health=False, access_audit=False)

    @staticmethod
    def _default_connect(connection_string: str):
        try:
            import snowflake.connector
        except ImportError as e:
            raise ImportError(
                "Snowflake support requires the 'snowflake-connector-python' driver, "
                "which is not installed. Install it with:  pip install sqldoc[snowflake]  "
                "(or:  pip install snowflake-connector-python)."
            ) from e
        u = urlparse(connection_string)
        parts = [p for p in (u.path or "").split("/") if p]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        return snowflake.connector.connect(
            account=u.hostname,
            user=unquote(u.username) if u.username else None,
            password=unquote(u.password) if u.password else None,
            database=parts[0] if parts else None,
            schema=parts[1] if len(parts) > 1 else None,
            warehouse=q.get("warehouse"),
            role=q.get("role"),
        )

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        # server carries the Snowflake account identifier.
        return f"snowflake://{username}:{password}@{server}/{database}"

    def cursor(self, conn):
        try:
            from snowflake.connector import DictCursor
            return conn.cursor(DictCursor)
        except ImportError:
            return conn.cursor()   # test fakes provide dict-style rows directly

    # --- tables ------------------------------------------------------------

    def extract_metadata(self) -> list[Table]:
        conn = self.connect()
        cursor = self.cursor(conn)

        cursor.execute("""
            SELECT table_schema AS "schema_name",
                   table_name AS "table_name",
                   row_count AS "row_count"
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema <> 'INFORMATION_SCHEMA'
            ORDER BY table_schema, table_name
        """)
        tables_raw = cursor.fetchall()

        pk_by_table = self._primary_keys(cursor)
        fk_by_table = self._foreign_keys(cursor)

        tables = []
        for row in tables_raw:
            schema_name, table_name = row["schema_name"], row["table_name"]
            pk_cols = pk_by_table.get((schema_name, table_name), set())
            fk_cols = fk_by_table.get((schema_name, table_name), {})
            columns = self._columns(cursor, schema_name, table_name, pk_cols, fk_cols)
            tables.append(Table(
                schema=schema_name,
                name=table_name,
                row_count=int(row["row_count"] or 0),
                columns=columns,
                indexes=[],             # Snowflake has no indexes
                triggers=[],            # Snowflake has no triggers
                check_constraints=[],
                unique_constraints=[],
            ))
        conn.close()
        return tables

    def _columns(self, cursor, schema_name, table_name, pk_cols, fk_cols) -> list[Column]:
        cursor.execute("""
            SELECT column_name AS "column_name",
                   data_type AS "data_type",
                   character_maximum_length AS "max_length",
                   is_nullable AS "is_nullable",
                   column_default AS "column_default",
                   comment AS "description"
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """, (schema_name, table_name))
        columns = []
        for r in cursor.fetchall():
            fk = fk_cols.get(r["column_name"])
            columns.append(Column(
                name=r["column_name"],
                data_type=r["data_type"],
                max_length=r["max_length"],
                is_nullable=(str(r["is_nullable"]).upper() == "YES"),
                is_primary_key=r["column_name"] in pk_cols,
                is_foreign_key=fk is not None,
                references_table=fk[0] if fk else None,
                references_column=fk[1] if fk else None,
                description=str(r["description"]) if r["description"] else None,
                default_definition=str(r["column_default"]) if r["column_default"] else None,
                fk_on_delete=fk[2] if fk else None,
                fk_on_update=fk[3] if fk else None,
            ))
        return columns

    def _primary_keys(self, cursor) -> dict:
        # SHOW PRIMARY KEYS exposes (schema_name, table_name, column_name).
        cursor.execute("SHOW PRIMARY KEYS IN DATABASE")
        out = {}
        for r in cursor.fetchall():
            key = (r["schema_name"], r["table_name"])
            out.setdefault(key, set()).add(r["column_name"])
        return out

    def _foreign_keys(self, cursor) -> dict:
        # SHOW IMPORTED KEYS exposes the FK side (fk_*) and the referenced PK side.
        cursor.execute("SHOW IMPORTED KEYS IN DATABASE")
        out = {}
        for r in cursor.fetchall():
            key = (r["fk_schema_name"], r["fk_table_name"])
            out.setdefault(key, {})[r["fk_column_name"]] = (
                r["pk_table_name"], r["pk_column_name"],
                r.get("delete_rule"), r.get("update_rule"),
            )
        return out

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        conn = self.connect()
        cursor = self.cursor(conn)
        cursor.execute("""
            SELECT table_schema AS "schema_name",
                   table_name AS "view_name",
                   view_definition AS "definition"
            FROM information_schema.views
            WHERE table_schema <> 'INFORMATION_SCHEMA'
            ORDER BY table_schema, table_name
        """)
        views_raw = cursor.fetchall()

        views = []
        for row in views_raw:
            cursor.execute("""
                SELECT column_name AS "column_name",
                       data_type AS "data_type",
                       character_maximum_length AS "max_length",
                       is_nullable AS "is_nullable"
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (row["schema_name"], row["view_name"]))
            columns = [
                Column(
                    name=cr["column_name"],
                    data_type=cr["data_type"],
                    max_length=cr["max_length"],
                    is_nullable=(str(cr["is_nullable"]).upper() == "YES"),
                    is_primary_key=False,
                    is_foreign_key=False,
                    references_table=None,
                    references_column=None,
                )
                for cr in cursor.fetchall()
            ]
            views.append(View(
                schema=row["schema_name"],
                name=row["view_name"],
                columns=columns,
                definition=str(row["definition"]) if row["definition"] else None,
            ))
        conn.close()
        return views

    # --- procedures --------------------------------------------------------

    def extract_procedures(self) -> list[StoredProcedure]:
        conn = self.connect()
        cursor = self.cursor(conn)
        cursor.execute("""
            SELECT procedure_schema AS "schema_name",
                   procedure_name AS "proc_name",
                   argument_signature AS "argument_signature",
                   comment AS "description"
            FROM information_schema.procedures
            WHERE procedure_schema <> 'INFORMATION_SCHEMA'
            ORDER BY procedure_schema, procedure_name
        """)
        procedures = []
        for r in cursor.fetchall():
            procedures.append(StoredProcedure(
                schema=r["schema_name"],
                name=r["proc_name"],
                parameters=_parse_arguments(r["argument_signature"]),
                definition=None,   # not exposed via INFORMATION_SCHEMA
                description=str(r["description"]) if r["description"] else None,
            ))
        conn.close()
        return procedures
