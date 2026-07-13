"""SQL Server / Azure SQL adapter.

Home of all Transact-SQL used for metadata extraction. The queries here are
the behavior-preserving descendants of the original `extractor.py` — the
catalog-view SQL (`sys.tables`, `sys.columns`, `sys.indexes`, ...) is unchanged;
only its packaging moved behind `DatabaseAdapter`. Connection acquisition goes
through `self.connect()` so the `sqldoc.extractor` shim can inject a patchable
connector for tests.

The DMV (`health`), aggregate-profiling (`quality`), and permission (`comply`
access audit) SQL still lives in its owning module for now; those modules
consume this adapter's `connect()` boundary and their relocation into concrete
adapters is the first step once a second dialect lands.
"""
import pyodbc

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities,
    Table, Column, Index, Trigger, CheckConstraint, UniqueConstraint,
    View, Parameter, StoredProcedure,
)


class SqlServerAdapter(DatabaseAdapter):
    dialect = "sqlserver"
    display_name = "SQL Server"
    # SQL Server is the reference implementation: everything is supported.
    capabilities = Capabilities(quality=True, health=True, access_audit=True,
                                server_monitoring=True, infra_monitoring=True)

    @staticmethod
    def _default_connect(connection_string: str):
        return pyodbc.connect(connection_string)

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        # Validate the interpolated parts so a value containing an ODBC
        # separator (;{}= or a newline) cannot inject extra connection
        # attributes. The password is not validated for content (it may legally
        # contain any character) but is brace-quoted below so it stays a single
        # attribute value; a literal closing brace is doubled per the ODBC spec.
        from sqldoc.validation import (validate_server, validate_database,
                                       validate_username)
        server = validate_server(server)
        database = validate_database(database)
        username = validate_username(username)
        pwd = "" if password is None else str(password)
        pwd_quoted = "{" + pwd.replace("}", "}}") + "}"
        return (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={server};"
            f"DATABASE={database};"
            f"UID={username};"
            f"PWD={pwd_quoted};"
            f"TrustServerCertificate=yes;"
        )

    def extract_metadata(self) -> list[Table]:
        conn = self.connect()
        cursor = conn.cursor()

        # Get all tables with row counts
        cursor.execute("""
            SELECT
                s.name AS schema_name,
                t.name AS table_name,
                p.rows AS row_count
            FROM sys.tables t
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.partitions p ON t.object_id = p.object_id
            WHERE p.index_id IN (0, 1)
            ORDER BY s.name, t.name
        """)
        tables_raw = cursor.fetchall()

        # DML triggers on tables (one query for the whole DB, grouped by table).
        # STRING_AGG collapses the per-event rows in sys.trigger_events.
        cursor.execute("""
            SELECT
                s.name AS schema_name,
                t.name AS table_name,
                tr.name AS trigger_name,
                tr.is_instead_of_trigger,
                tr.is_disabled,
                m.definition,
                (SELECT STRING_AGG(te.type_desc, ',')
                 FROM sys.trigger_events te WHERE te.object_id = tr.object_id) AS events
            FROM sys.triggers tr
            INNER JOIN sys.tables t ON tr.parent_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            LEFT JOIN sys.sql_modules m ON tr.object_id = m.object_id
            WHERE tr.parent_class = 1 AND tr.is_ms_shipped = 0
            ORDER BY s.name, t.name, tr.name
        """)
        triggers_by_table = {}
        for row in cursor.fetchall():
            triggers_by_table.setdefault((row.schema_name, row.table_name), []).append(Trigger(
                name=row.trigger_name,
                is_instead_of=bool(row.is_instead_of_trigger),
                is_disabled=bool(row.is_disabled),
                events=[e for e in (row.events or "").split(",") if e],
                definition=str(row.definition) if row.definition else None,
            ))

        tables = []
        for schema_name, table_name, row_count in tables_raw:
            cursor.execute("""
                SELECT
                    c.name AS column_name,
                    tp.name AS data_type,
                    c.max_length,
                    c.is_nullable,
                    CASE WHEN pk.column_id IS NOT NULL THEN 1 ELSE 0 END AS is_primary_key,
                    CASE WHEN fk.parent_column_id IS NOT NULL THEN 1 ELSE 0 END AS is_foreign_key,
                    rt.name AS references_table,
                    rc.name AS references_column,
                    ep.value AS description,
                    c.is_computed,
                    cc.definition AS computed_definition,
                    dc.definition AS default_definition,
                    fko.delete_referential_action_desc AS fk_on_delete,
                    fko.update_referential_action_desc AS fk_on_update
                FROM sys.columns c
                INNER JOIN sys.types tp ON c.user_type_id = tp.user_type_id
                INNER JOIN sys.tables t ON c.object_id = t.object_id
                INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
                LEFT JOIN (
                    SELECT DISTINCT ic.object_id, ic.column_id
                    FROM sys.index_columns ic
                    INNER JOIN sys.indexes i ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                    WHERE i.is_primary_key = 1
                ) pk ON c.object_id = pk.object_id AND c.column_id = pk.column_id
                LEFT JOIN sys.foreign_key_columns fk ON c.object_id = fk.parent_object_id AND c.column_id = fk.parent_column_id
                LEFT JOIN sys.foreign_keys fko ON fk.constraint_object_id = fko.object_id
                LEFT JOIN sys.tables rt ON fk.referenced_object_id = rt.object_id
                LEFT JOIN sys.columns rc ON fk.referenced_object_id = rc.object_id AND fk.referenced_column_id = rc.column_id
                LEFT JOIN sys.extended_properties ep ON ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description'
                LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
                LEFT JOIN sys.default_constraints dc ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
                WHERE t.name = ? AND s.name = ?
                ORDER BY c.column_id
            """, table_name, schema_name)

            columns_raw = cursor.fetchall()
            columns = []
            seen_columns = set()
            for row in columns_raw:
                if row.column_name in seen_columns:
                    continue
                seen_columns.add(row.column_name)
                columns.append(Column(
                    name=row.column_name,
                    data_type=row.data_type,
                    max_length=row.max_length,
                    is_nullable=bool(row.is_nullable),
                    is_primary_key=bool(row.is_primary_key),
                    is_foreign_key=bool(row.is_foreign_key),
                    references_table=row.references_table,
                    references_column=row.references_column,
                    description=str(row.description) if row.description else None,
                    is_computed=bool(row.is_computed),
                    computed_definition=str(row.computed_definition) if row.computed_definition else None,
                    default_definition=str(row.default_definition) if row.default_definition else None,
                    fk_on_delete=str(row.fk_on_delete) if row.fk_on_delete else None,
                    fk_on_update=str(row.fk_on_update) if row.fk_on_update else None,
                ))

            # Indexes on this table. One row per (index, column); grouped below.
            cursor.execute("""
                SELECT
                    i.name AS index_name,
                    i.type_desc,
                    i.is_unique,
                    i.is_primary_key,
                    c.name AS column_name,
                    ic.is_included_column,
                    ic.key_ordinal
                FROM sys.indexes i
                INNER JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
                INNER JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                INNER JOIN sys.tables t ON i.object_id = t.object_id
                INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
                WHERE t.name = ? AND s.name = ? AND i.type > 0 AND i.name IS NOT NULL
                ORDER BY i.name, ic.is_included_column, ic.key_ordinal, ic.index_column_id
            """, table_name, schema_name)

            indexes_by_name = {}
            for row in cursor.fetchall():
                idx = indexes_by_name.get(row.index_name)
                if idx is None:
                    idx = Index(
                        name=row.index_name,
                        type_desc=row.type_desc,
                        is_unique=bool(row.is_unique),
                        is_primary_key=bool(row.is_primary_key),
                    )
                    indexes_by_name[row.index_name] = idx
                if row.is_included_column:
                    idx.included_columns.append(row.column_name)
                else:
                    idx.key_columns.append(row.column_name)

            # CHECK constraints. parent_column_id = 0 means a table-level check;
            # otherwise it belongs to the named column.
            cursor.execute("""
                SELECT
                    chk.name AS check_name,
                    chk.definition AS check_definition,
                    col.name AS column_name
                FROM sys.check_constraints chk
                INNER JOIN sys.tables t ON chk.parent_object_id = t.object_id
                INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
                LEFT JOIN sys.columns col ON col.object_id = chk.parent_object_id AND col.column_id = chk.parent_column_id
                WHERE t.name = ? AND s.name = ?
                ORDER BY chk.name
            """, table_name, schema_name)
            check_constraints = [
                CheckConstraint(name=row.check_name,
                                definition=str(row.check_definition) if row.check_definition else "",
                                column=row.column_name)
                for row in cursor.fetchall()
            ]

            # UNIQUE constraints (type 'UQ' in sys.key_constraints, backed by a
            # unique index whose columns we read from sys.index_columns). One row per
            # (constraint, column); grouped below.
            cursor.execute("""
                SELECT
                    kc.name AS uq_name,
                    col.name AS column_name
                FROM sys.key_constraints kc
                INNER JOIN sys.tables t ON kc.parent_object_id = t.object_id
                INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
                INNER JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
                INNER JOIN sys.columns col ON col.object_id = ic.object_id AND col.column_id = ic.column_id
                WHERE kc.type = 'UQ' AND t.name = ? AND s.name = ?
                ORDER BY kc.name, ic.key_ordinal
            """, table_name, schema_name)
            uniques_by_name = {}
            for row in cursor.fetchall():
                uq = uniques_by_name.get(row.uq_name)
                if uq is None:
                    uq = UniqueConstraint(name=row.uq_name)
                    uniques_by_name[row.uq_name] = uq
                uq.columns.append(row.column_name)

            tables.append(Table(
                schema=schema_name,
                name=table_name,
                row_count=row_count,
                columns=columns,
                indexes=list(indexes_by_name.values()),
                triggers=triggers_by_table.get((schema_name, table_name), []),
                check_constraints=check_constraints,
                unique_constraints=list(uniques_by_name.values()),
            ))

        conn.close()
        return tables

    def extract_views(self) -> list[View]:
        conn = self.connect()
        cursor = conn.cursor()

        # Views + their SQL definition and any MS_Description. minor_id = 0 targets
        # the object itself (not a column) in sys.extended_properties.
        cursor.execute("""
            SELECT
                s.name AS schema_name,
                v.name AS view_name,
                m.definition AS definition,
                ep.value AS description
            FROM sys.views v
            INNER JOIN sys.schemas s ON v.schema_id = s.schema_id
            LEFT JOIN sys.sql_modules m ON v.object_id = m.object_id
            LEFT JOIN sys.extended_properties ep
                ON ep.major_id = v.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
            ORDER BY s.name, v.name
        """)
        views_raw = cursor.fetchall()

        views = []
        for schema_name, view_name, definition, description in views_raw:
            cursor.execute("""
                SELECT
                    c.name AS column_name,
                    tp.name AS data_type,
                    c.max_length,
                    c.is_nullable,
                    ep.value AS description
                FROM sys.columns c
                INNER JOIN sys.types tp ON c.user_type_id = tp.user_type_id
                INNER JOIN sys.views v ON c.object_id = v.object_id
                INNER JOIN sys.schemas s ON v.schema_id = s.schema_id
                LEFT JOIN sys.extended_properties ep
                    ON ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description'
                WHERE v.name = ? AND s.name = ?
                ORDER BY c.column_id
            """, view_name, schema_name)

            columns = []
            seen_columns = set()
            for row in cursor.fetchall():
                if row.column_name in seen_columns:
                    continue
                seen_columns.add(row.column_name)
                columns.append(Column(
                    name=row.column_name,
                    data_type=row.data_type,
                    max_length=row.max_length,
                    is_nullable=bool(row.is_nullable),
                    is_primary_key=False,
                    is_foreign_key=False,
                    references_table=None,
                    references_column=None,
                    description=str(row.description) if row.description else None,
                ))

            views.append(View(
                schema=schema_name,
                name=view_name,
                columns=columns,
                definition=str(definition) if definition else None,
                description=str(description) if description else None,
            ))

        conn.close()
        return views

    def extract_procedures(self) -> list[StoredProcedure]:
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                s.name AS schema_name,
                p.name AS proc_name,
                m.definition AS definition,
                ep.value AS description
            FROM sys.procedures p
            INNER JOIN sys.schemas s ON p.schema_id = s.schema_id
            LEFT JOIN sys.sql_modules m ON p.object_id = m.object_id
            LEFT JOIN sys.extended_properties ep
                ON ep.major_id = p.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
            ORDER BY s.name, p.name
        """)
        procs_raw = cursor.fetchall()

        procedures = []
        for schema_name, proc_name, definition, description in procs_raw:
            cursor.execute("""
                SELECT
                    pm.name AS param_name,
                    tp.name AS data_type,
                    pm.max_length,
                    pm.is_output
                FROM sys.parameters pm
                INNER JOIN sys.types tp ON pm.user_type_id = tp.user_type_id
                INNER JOIN sys.procedures p ON pm.object_id = p.object_id
                INNER JOIN sys.schemas s ON p.schema_id = s.schema_id
                WHERE p.name = ? AND s.name = ?
                ORDER BY pm.parameter_id
            """, proc_name, schema_name)

            parameters = []
            for row in cursor.fetchall():
                # The implicit return-value parameter of a proc has an empty name.
                if not row.param_name:
                    continue
                parameters.append(Parameter(
                    name=row.param_name,
                    data_type=row.data_type,
                    max_length=row.max_length,
                    is_output=bool(row.is_output),
                ))

            procedures.append(StoredProcedure(
                schema=schema_name,
                name=proc_name,
                parameters=parameters,
                definition=str(definition) if definition else None,
                description=str(description) if description else None,
            ))

        conn.close()
        return procedures
