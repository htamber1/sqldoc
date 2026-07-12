"""MySQL adapter.

Populates the shared dialect-neutral dataclasses from `information_schema`. The
driver (`mysql-connector-python`) is an *optional* dependency imported lazily
inside `_default_connect`, so SQL Server users never need it installed; a
missing driver raises a clear, actionable error.

Rows are read through a `dictionary=True` cursor (`row["col"]`) — the one row
format mysql-connector's C-extension and pure-Python connections both support
across versions (the older `named_tuple` cursor was dropped in 9.x). Every
SELECT aliases its columns, so the dict keys are stable regardless of engine.

MySQL has no schema/database distinction, so `Table.schema` carries the database
name and every query is scoped to `DATABASE()` (the connected database). MySQL
has neither INCLUDE index columns nor INSTEAD OF / disablable triggers, so those
fields are populated with their only valid values. CHECK constraints require
MySQL 8.0.16+; the query degrades to none on older servers.
"""
from urllib.parse import unquote, urlparse

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities,
    Table, Column, Index, Trigger, CheckConstraint, UniqueConstraint,
    View, Parameter, StoredProcedure,
)


class MySQLAdapter(DatabaseAdapter):
    dialect = "mysql"
    display_name = "MySQL"
    # Metadata + aggregate profiling (quality) + health via performance_schema +
    # access audit via information_schema.table_privileges.
    capabilities = Capabilities(quality=True, health=True, access_audit=True,
                                infra_monitoring=True)

    @staticmethod
    def _default_connect(connection_string: str):
        try:
            import mysql.connector
        except ImportError as e:
            raise ImportError(
                "MySQL support requires the 'mysql-connector-python' driver, which "
                "is not installed. Install it with:  pip install sqldoc[mysql]  "
                "(or:  pip install mysql-connector-python)."
            ) from e
        u = urlparse(connection_string)
        return mysql.connector.connect(
            host=u.hostname or "localhost",
            port=u.port or 3306,
            user=unquote(u.username) if u.username else None,
            password=unquote(u.password) if u.password else None,
            database=(u.path or "").lstrip("/") or None,
        )

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        return f"mysql://{username}:{password}@{server}/{database}"

    def cursor(self, conn):
        # dictionary=True yields row["col"] access — the format supported across
        # mysql-connector's C-extension and pure-Python connections and versions.
        return conn.cursor(dictionary=True)

    # --- tables ------------------------------------------------------------

    def extract_metadata(self) -> list[Table]:
        conn = self.connect()
        cursor = self.cursor(conn)

        cursor.execute("""
            SELECT table_schema AS schema_name,
                   table_name AS table_name,
                   table_rows AS row_count
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE' AND table_schema = DATABASE()
            ORDER BY table_name
        """)
        tables_raw = cursor.fetchall()

        # Triggers for the connected DB. Each MySQL trigger is a single
        # timing+event, so one row == one Trigger.
        cursor.execute("""
            SELECT trigger_schema AS schema_name,
                   event_object_table AS table_name,
                   trigger_name AS trigger_name,
                   action_timing AS action_timing,
                   event_manipulation AS event_manipulation,
                   action_statement AS action_statement
            FROM information_schema.triggers
            WHERE trigger_schema = DATABASE()
            ORDER BY event_object_table, trigger_name
        """)
        triggers_by_table = {}
        for row in cursor.fetchall():
            triggers_by_table.setdefault((row["schema_name"], row["table_name"]), []).append(Trigger(
                name=row["trigger_name"],
                is_instead_of=False,           # MySQL has no INSTEAD OF triggers
                is_disabled=False,             # MySQL triggers cannot be disabled
                events=[row["event_manipulation"]] if row["event_manipulation"] else [],
                definition=str(row["action_statement"]) if row["action_statement"] else None,
            ))

        tables = []
        for row in tables_raw:
            schema_name, table_name = row["schema_name"], row["table_name"]
            columns = self._columns(cursor, schema_name, table_name)
            indexes = self._indexes(cursor, schema_name, table_name)
            checks = self._check_constraints(cursor, schema_name, table_name)
            uniques = self._unique_constraints(cursor, schema_name, table_name)
            tables.append(Table(
                schema=schema_name,
                name=table_name,
                row_count=int(row["row_count"] or 0),
                columns=columns,
                indexes=indexes,
                triggers=triggers_by_table.get((schema_name, table_name), []),
                check_constraints=checks,
                unique_constraints=uniques,
            ))

        conn.close()
        return tables

    def _columns(self, cursor, schema_name, table_name) -> list[Column]:
        cursor.execute("""
            SELECT column_name AS column_name,
                   data_type AS data_type,
                   character_maximum_length AS max_length,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   column_key AS column_key,
                   generation_expression AS generation_expression,
                   column_comment AS description
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """, (schema_name, table_name))
        cols_raw = cursor.fetchall()
        fk_by_col = self._fk_columns(cursor, schema_name, table_name)

        columns = []
        for row in cols_raw:
            fk = fk_by_col.get(row["column_name"])
            gen = str(row["generation_expression"]) if row["generation_expression"] else ""
            columns.append(Column(
                name=row["column_name"],
                data_type=row["data_type"],
                max_length=row["max_length"],
                is_nullable=(str(row["is_nullable"]).upper() == "YES"),
                is_primary_key=(str(row["column_key"]).upper() == "PRI"),
                is_foreign_key=fk is not None,
                references_table=fk[0] if fk else None,
                references_column=fk[1] if fk else None,
                description=str(row["description"]) if row["description"] else None,
                is_computed=bool(gen),
                computed_definition=gen or None,
                default_definition=str(row["column_default"]) if row["column_default"] else None,
                fk_on_delete=fk[2] if fk else None,
                fk_on_update=fk[3] if fk else None,
            ))
        return columns

    def _fk_columns(self, cursor, schema_name, table_name) -> dict:
        cursor.execute("""
            SELECT kcu.column_name AS column_name,
                   kcu.referenced_table_name AS referenced_table_name,
                   kcu.referenced_column_name AS referenced_column_name,
                   rc.delete_rule AS delete_rule,
                   rc.update_rule AS update_rule
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.referential_constraints rc
              ON rc.constraint_schema = kcu.constraint_schema
             AND rc.constraint_name = kcu.constraint_name
            WHERE kcu.table_schema = %s AND kcu.table_name = %s
              AND kcu.referenced_table_name IS NOT NULL
        """, (schema_name, table_name))
        out = {}
        for r in cursor.fetchall():
            out[r["column_name"]] = (r["referenced_table_name"], r["referenced_column_name"],
                                     r["delete_rule"], r["update_rule"])
        return out

    def _indexes(self, cursor, schema_name, table_name) -> list[Index]:
        cursor.execute("""
            SELECT index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name,
                   index_type AS index_type
            FROM information_schema.statistics
            WHERE table_schema = %s AND table_name = %s
            ORDER BY index_name, seq_in_index
        """, (schema_name, table_name))
        by_name = {}
        for row in cursor.fetchall():
            name = row["index_name"]
            idx = by_name.get(name)
            if idx is None:
                idx = Index(
                    name=name,
                    type_desc=str(row["index_type"]).upper() if row["index_type"] else "",
                    is_unique=(int(row["non_unique"]) == 0),
                    is_primary_key=(str(name).upper() == "PRIMARY"),
                )
                by_name[name] = idx
            idx.key_columns.append(row["column_name"])   # MySQL has no INCLUDE columns
        return list(by_name.values())

    def _check_constraints(self, cursor, schema_name, table_name) -> list[CheckConstraint]:
        # information_schema.check_constraints requires MySQL 8.0.16+; degrade to
        # none on older servers rather than failing the whole extraction.
        try:
            cursor.execute("""
                SELECT tc.constraint_name AS constraint_name,
                       cc.check_clause AS check_clause
                FROM information_schema.table_constraints tc
                JOIN information_schema.check_constraints cc
                  ON cc.constraint_schema = tc.table_schema
                 AND cc.constraint_name = tc.constraint_name
                WHERE tc.constraint_type = 'CHECK'
                  AND tc.table_schema = %s AND tc.table_name = %s
                ORDER BY tc.constraint_name
            """, (schema_name, table_name))
            return [
                CheckConstraint(name=r["constraint_name"],
                                definition=str(r["check_clause"]) if r["check_clause"] else "",
                                column=None)
                for r in cursor.fetchall()
            ]
        except Exception:
            return []

    def _unique_constraints(self, cursor, schema_name, table_name) -> list[UniqueConstraint]:
        cursor.execute("""
            SELECT tc.constraint_name AS uq_name,
                   kcu.column_name AS column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_schema = tc.table_schema
             AND kcu.constraint_name = tc.constraint_name
             AND kcu.table_name = tc.table_name
            WHERE tc.constraint_type = 'UNIQUE'
              AND tc.table_schema = %s AND tc.table_name = %s
            ORDER BY tc.constraint_name, kcu.ordinal_position
        """, (schema_name, table_name))
        by_name = {}
        for r in cursor.fetchall():
            name = r["uq_name"]
            uq = by_name.get(name)
            if uq is None:
                uq = UniqueConstraint(name=name)
                by_name[name] = uq
            uq.columns.append(r["column_name"])
        return list(by_name.values())

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        conn = self.connect()
        cursor = self.cursor(conn)
        cursor.execute("""
            SELECT table_schema AS schema_name,
                   table_name AS view_name,
                   view_definition AS definition
            FROM information_schema.views
            WHERE table_schema = DATABASE()
            ORDER BY table_name
        """)
        views_raw = cursor.fetchall()

        views = []
        for row in views_raw:
            cursor.execute("""
                SELECT column_name AS column_name,
                       data_type AS data_type,
                       character_maximum_length AS max_length,
                       is_nullable AS is_nullable
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

    # --- procedures + functions --------------------------------------------

    def extract_procedures(self) -> list[StoredProcedure]:
        conn = self.connect()
        cursor = self.cursor(conn)
        cursor.execute("""
            SELECT routine_schema AS schema_name,
                   routine_name AS proc_name,
                   specific_name AS specific_name,
                   routine_definition AS definition,
                   routine_comment AS description
            FROM information_schema.routines
            WHERE routine_schema = DATABASE()
            ORDER BY routine_name
        """)
        procs_raw = cursor.fetchall()

        procedures = []
        for row in procs_raw:
            cursor.execute("""
                SELECT parameter_name AS parameter_name,
                       data_type AS data_type,
                       character_maximum_length AS max_length,
                       parameter_mode AS parameter_mode
                FROM information_schema.parameters
                WHERE specific_schema = %s AND specific_name = %s
                ORDER BY ordinal_position
            """, (row["schema_name"], row["specific_name"]))
            parameters = []
            for pr in cursor.fetchall():
                # ordinal_position 0 (the function return value) has a NULL name.
                if not pr["parameter_name"]:
                    continue
                parameters.append(Parameter(
                    name=pr["parameter_name"],
                    data_type=pr["data_type"],
                    max_length=pr["max_length"],
                    is_output=str(pr["parameter_mode"]).upper() in ("OUT", "INOUT"),
                ))
            procedures.append(StoredProcedure(
                schema=row["schema_name"],
                name=row["proc_name"],
                parameters=parameters,
                definition=str(row["definition"]) if row["definition"] else None,
                description=str(row["description"]) if row["description"] else None,
            ))
        conn.close()
        return procedures
