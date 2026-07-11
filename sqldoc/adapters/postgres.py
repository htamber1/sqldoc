"""PostgreSQL adapter.

Populates the shared dialect-neutral dataclasses from `information_schema` +
`pg_catalog`. The driver (`psycopg2`) is an *optional* dependency imported
lazily inside `_default_connect`, so SQL Server users never need it installed;
a missing driver raises a clear, actionable error.

Row counts come from `pg_class.reltuples` (a planner estimate refreshed by
ANALYZE), mirroring the estimate-based `sys.partitions.rows` used for SQL
Server. Structured index key/included columns need `pg_index.indnkeyatts`
(PostgreSQL 11+); generated columns need `information_schema.columns
.is_generated` (PostgreSQL 12+).
"""
from urllib.parse import unquote, urlparse

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities,
    Table, Column, Index, Trigger, CheckConstraint, UniqueConstraint,
    View, Parameter, StoredProcedure,
)

# pg_trigger.tgtype is a bitmask (see PostgreSQL src/include/catalog/pg_trigger.h)
_TG_ROW = 1 << 0
_TG_BEFORE = 1 << 1
_TG_INSERT = 1 << 2
_TG_DELETE = 1 << 3
_TG_UPDATE = 1 << 4
_TG_INSTEAD = 1 << 6


def _decode_trigger_events(tgtype: int) -> list[str]:
    events = []
    if tgtype & _TG_INSERT:
        events.append("INSERT")
    if tgtype & _TG_UPDATE:
        events.append("UPDATE")
    if tgtype & _TG_DELETE:
        events.append("DELETE")
    return events


class PostgresAdapter(DatabaseAdapter):
    dialect = "postgres"
    display_name = "PostgreSQL"
    # Metadata + aggregate profiling (quality) + health via pg_stat_* views +
    # access audit via information_schema.table_privileges.
    capabilities = Capabilities(quality=True, health=True, access_audit=True)

    @staticmethod
    def _default_connect(connection_string: str):
        try:
            import psycopg2
            from psycopg2.extras import NamedTupleCursor
        except ImportError as e:
            raise ImportError(
                "PostgreSQL support requires the 'psycopg2' driver, which is not "
                "installed. Install it with:  pip install sqldoc[postgres]  "
                "(or:  pip install psycopg2-binary)."
            ) from e
        # NamedTupleCursor as the default factory gives row.column attribute
        # access, matching the pyodbc-style access the extraction code uses.
        conn = psycopg2.connect(connection_string, cursor_factory=NamedTupleCursor)
        # Autocommit for read-only introspection/analysis: without it, one failed
        # statement (e.g. a missing pg_stat_statements extension, or MIN on an
        # unsupported type) aborts the whole transaction and every following
        # query fails with InFailedSqlTransaction. Each analysis query is
        # independently guarded, so per-statement autocommit is what we want.
        conn.autocommit = True
        return conn

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        return f"postgresql://{username}:{password}@{server}/{database}"

    # --- tables ------------------------------------------------------------

    def extract_metadata(self) -> list[Table]:
        conn = self.connect()
        cursor = conn.cursor()

        # Ordinary tables ('r') and declarative-partition parents ('p'), with
        # estimated row counts (reltuples; refreshed by ANALYZE). Exclude the
        # physical partition children (relispartition) so a partitioned table
        # documents as one logical table, not one per partition. A partition
        # parent's reltuples is -1 (no direct storage); clamp it to 0.
        cursor.execute("""
            SELECT n.nspname AS schema_name,
                   c.relname AS table_name,
                   GREATEST(c.reltuples, 0)::bigint AS row_count
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND NOT c.relispartition
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, c.relname
        """)
        tables_raw = cursor.fetchall()

        # All non-internal triggers for the DB, grouped by table.
        cursor.execute("""
            SELECT n.nspname AS schema_name,
                   c.relname AS table_name,
                   t.tgname AS trigger_name,
                   t.tgtype,
                   t.tgenabled,
                   pg_catalog.pg_get_triggerdef(t.oid) AS definition
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, c.relname, t.tgname
        """)
        triggers_by_table = {}
        for row in cursor.fetchall():
            triggers_by_table.setdefault((row.schema_name, row.table_name), []).append(Trigger(
                name=row.trigger_name,
                is_instead_of=bool(int(row.tgtype) & _TG_INSTEAD),
                is_disabled=(row.tgenabled == 'D'),
                events=_decode_trigger_events(int(row.tgtype)),
                definition=str(row.definition) if row.definition else None,
            ))

        tables = []
        for schema_name, table_name, row_count in tables_raw:
            columns = self._columns(cursor, schema_name, table_name)
            indexes = self._indexes(cursor, schema_name, table_name)
            checks = self._check_constraints(cursor, schema_name, table_name)
            uniques = self._unique_constraints(cursor, schema_name, table_name)
            tables.append(Table(
                schema=schema_name,
                name=table_name,
                row_count=int(row_count or 0),
                columns=columns,
                indexes=indexes,
                triggers=triggers_by_table.get((schema_name, table_name), []),
                check_constraints=checks,
                unique_constraints=uniques,
            ))

        conn.close()
        return tables

    def _columns(self, cursor, schema_name, table_name) -> list[Column]:
        # Column definitions incl. generated-column expression and any comment.
        cursor.execute("""
            SELECT c.column_name,
                   c.data_type,
                   c.character_maximum_length AS max_length,
                   c.is_nullable,
                   c.column_default,
                   c.is_generated,
                   c.generation_expression,
                   (SELECT pd.description
                      FROM pg_catalog.pg_description pd
                      JOIN pg_catalog.pg_class pc ON pc.oid = pd.objoid
                      JOIN pg_catalog.pg_namespace pn ON pn.oid = pc.relnamespace
                     WHERE pn.nspname = c.table_schema
                       AND pc.relname = c.table_name
                       AND pd.objsubid = c.ordinal_position) AS description
            FROM information_schema.columns c
            WHERE c.table_schema = %s AND c.table_name = %s
            ORDER BY c.ordinal_position
        """, (schema_name, table_name))
        cols_raw = cursor.fetchall()

        pk_cols = self._pk_columns(cursor, schema_name, table_name)
        fk_by_col = self._fk_columns(cursor, schema_name, table_name)

        columns = []
        for row in cols_raw:
            fk = fk_by_col.get(row.column_name)
            is_generated = str(getattr(row, "is_generated", "NEVER")).upper() == "ALWAYS"
            columns.append(Column(
                name=row.column_name,
                data_type=row.data_type,
                max_length=row.max_length,
                is_nullable=(str(row.is_nullable).upper() == "YES"),
                is_primary_key=row.column_name in pk_cols,
                is_foreign_key=fk is not None,
                references_table=fk[0] if fk else None,
                references_column=fk[1] if fk else None,
                description=str(row.description) if row.description else None,
                is_computed=is_generated,
                computed_definition=(str(row.generation_expression)
                                     if is_generated and row.generation_expression else None),
                default_definition=str(row.column_default) if row.column_default else None,
                fk_on_delete=fk[2] if fk else None,
                fk_on_update=fk[3] if fk else None,
            ))
        return columns

    def _pk_columns(self, cursor, schema_name, table_name) -> set:
        cursor.execute("""
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = %s AND tc.table_name = %s
        """, (schema_name, table_name))
        return {r.column_name for r in cursor.fetchall()}

    def _fk_columns(self, cursor, schema_name, table_name) -> dict:
        cursor.execute("""
            SELECT kcu.column_name,
                   ccu.table_name AS foreign_table_name,
                   ccu.column_name AS foreign_column_name,
                   rc.delete_rule,
                   rc.update_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.constraint_schema = tc.constraint_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.constraint_schema
            JOIN information_schema.referential_constraints rc
              ON rc.constraint_name = tc.constraint_name
             AND rc.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s AND tc.table_name = %s
        """, (schema_name, table_name))
        out = {}
        for r in cursor.fetchall():
            out[r.column_name] = (r.foreign_table_name, r.foreign_column_name,
                                  r.delete_rule, r.update_rule)
        return out

    def _indexes(self, cursor, schema_name, table_name) -> list[Index]:
        # Structured index columns via pg_index; indnkeyatts (PG 11+) separates
        # key columns from INCLUDE columns. am.amname gives the index method.
        cursor.execute("""
            SELECT i.relname AS index_name,
                   am.amname AS index_type,
                   ix.indisunique AS is_unique,
                   ix.indisprimary AS is_primary_key,
                   a.attname AS column_name,
                   (k.ord > ix.indnkeyatts) AS is_included
            FROM pg_catalog.pg_class t
            JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_catalog.pg_index ix ON ix.indrelid = t.oid
            JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
            JOIN pg_catalog.pg_am am ON am.oid = i.relam
            JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_catalog.pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE n.nspname = %s AND t.relname = %s
            ORDER BY i.relname, k.ord
        """, (schema_name, table_name))
        by_name = {}
        for row in cursor.fetchall():
            idx = by_name.get(row.index_name)
            if idx is None:
                idx = Index(
                    name=row.index_name,
                    type_desc=str(row.index_type).upper() if row.index_type else "",
                    is_unique=bool(row.is_unique),
                    is_primary_key=bool(row.is_primary_key),
                )
                by_name[row.index_name] = idx
            if row.is_included:
                idx.included_columns.append(row.column_name)
            else:
                idx.key_columns.append(row.column_name)
        return list(by_name.values())

    def _check_constraints(self, cursor, schema_name, table_name) -> list[CheckConstraint]:
        # Exclude the implicit NOT NULL check constraints Postgres surfaces here.
        cursor.execute("""
            SELECT tc.constraint_name, cc.check_clause
            FROM information_schema.table_constraints tc
            JOIN information_schema.check_constraints cc
              ON cc.constraint_name = tc.constraint_name
             AND cc.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'CHECK'
              AND tc.table_schema = %s AND tc.table_name = %s
              AND cc.check_clause NOT LIKE '%% IS NOT NULL'
            ORDER BY tc.constraint_name
        """, (schema_name, table_name))
        return [
            CheckConstraint(name=r.constraint_name,
                            definition=str(r.check_clause) if r.check_clause else "",
                            column=None)
            for r in cursor.fetchall()
        ]

    def _unique_constraints(self, cursor, schema_name, table_name) -> list[UniqueConstraint]:
        cursor.execute("""
            SELECT tc.constraint_name AS uq_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'UNIQUE'
              AND tc.table_schema = %s AND tc.table_name = %s
            ORDER BY tc.constraint_name, kcu.ordinal_position
        """, (schema_name, table_name))
        by_name = {}
        for r in cursor.fetchall():
            uq = by_name.get(r.uq_name)
            if uq is None:
                uq = UniqueConstraint(name=r.uq_name)
                by_name[r.uq_name] = uq
            uq.columns.append(r.column_name)
        return list(by_name.values())

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT schemaname AS schema_name,
                   viewname AS view_name,
                   definition
            FROM pg_catalog.pg_views
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY schemaname, viewname
        """)
        views_raw = cursor.fetchall()

        views = []
        for row in views_raw:
            # information_schema.columns also covers views.
            cursor.execute("""
                SELECT c.column_name,
                       c.data_type,
                       c.character_maximum_length AS max_length,
                       c.is_nullable
                FROM information_schema.columns c
                WHERE c.table_schema = %s AND c.table_name = %s
                ORDER BY c.ordinal_position
            """, (row.schema_name, row.view_name))
            columns = [
                Column(
                    name=cr.column_name,
                    data_type=cr.data_type,
                    max_length=cr.max_length,
                    is_nullable=(str(cr.is_nullable).upper() == "YES"),
                    is_primary_key=False,
                    is_foreign_key=False,
                    references_table=None,
                    references_column=None,
                )
                for cr in cursor.fetchall()
            ]
            views.append(View(
                schema=row.schema_name,
                name=row.view_name,
                columns=columns,
                definition=str(row.definition) if row.definition else None,
            ))
        conn.close()
        return views

    # --- functions + procedures --------------------------------------------

    def extract_procedures(self) -> list[StoredProcedure]:
        conn = self.connect()
        cursor = conn.cursor()
        # prokind 'f' = function, 'p' = procedure (PG 11+). specific_name in the
        # SQL standard views is proname||'_'||oid, which we rebuild to match
        # information_schema.parameters.
        cursor.execute("""
            SELECT n.nspname AS schema_name,
                   p.proname AS proc_name,
                   p.oid AS oid,
                   pg_catalog.pg_get_functiondef(p.oid) AS definition,
                   pg_catalog.obj_description(p.oid, 'pg_proc') AS description
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND p.prokind IN ('f', 'p')
            ORDER BY n.nspname, p.proname
        """)
        procs_raw = cursor.fetchall()

        procedures = []
        for row in procs_raw:
            specific_name = f"{row.proc_name}_{row.oid}"
            cursor.execute("""
                SELECT parameter_name,
                       data_type,
                       character_maximum_length AS max_length,
                       parameter_mode
                FROM information_schema.parameters
                WHERE specific_schema = %s AND specific_name = %s
                ORDER BY ordinal_position
            """, (row.schema_name, specific_name))
            parameters = []
            for pr in cursor.fetchall():
                if not pr.parameter_name:
                    continue
                parameters.append(Parameter(
                    name=pr.parameter_name,
                    data_type=pr.data_type,
                    max_length=pr.max_length,
                    is_output=str(pr.parameter_mode).upper() in ("OUT", "INOUT"),
                ))
            procedures.append(StoredProcedure(
                schema=row.schema_name,
                name=row.proc_name,
                parameters=parameters,
                definition=str(row.definition) if row.definition else None,
                description=str(row.description) if row.description else None,
            ))
        conn.close()
        return procedures
