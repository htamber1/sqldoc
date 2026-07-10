import pyodbc
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Column:
    name: str
    data_type: str
    max_length: Optional[int]
    is_nullable: bool
    is_primary_key: bool
    is_foreign_key: bool
    references_table: Optional[str]
    references_column: Optional[str]
    description: Optional[str] = None
    is_computed: bool = False
    computed_definition: Optional[str] = None

@dataclass
class Index:
    name: str
    type_desc: str          # CLUSTERED / NONCLUSTERED / HEAP
    is_unique: bool
    is_primary_key: bool
    key_columns: list[str] = field(default_factory=list)
    included_columns: list[str] = field(default_factory=list)

@dataclass
class Trigger:
    name: str
    is_instead_of: bool     # INSTEAD OF vs AFTER
    is_disabled: bool
    events: list[str] = field(default_factory=list)   # INSERT / UPDATE / DELETE
    definition: Optional[str] = None

@dataclass
class Table:
    schema: str
    name: str
    row_count: int
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    description: Optional[str] = None

@dataclass
class View:
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)
    definition: Optional[str] = None
    description: Optional[str] = None

@dataclass
class Parameter:
    name: str
    data_type: str
    max_length: Optional[int]
    is_output: bool

@dataclass
class StoredProcedure:
    schema: str
    name: str
    parameters: list[Parameter] = field(default_factory=list)
    definition: Optional[str] = None
    description: Optional[str] = None

def build_connection_string(server: str, database: str, username: str, password: str) -> str:
    return (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        f"TrustServerCertificate=yes;"
    )

def get_connection(connection_string: str):
    return pyodbc.connect(connection_string)

def extract_metadata(connection_string: str) -> list[Table]:
    conn = get_connection(connection_string)
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
                cc.definition AS computed_definition
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
            LEFT JOIN sys.tables rt ON fk.referenced_object_id = rt.object_id
            LEFT JOIN sys.columns rc ON fk.referenced_object_id = rc.object_id AND fk.referenced_column_id = rc.column_id
            LEFT JOIN sys.extended_properties ep ON ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description'
            LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
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

        tables.append(Table(
            schema=schema_name,
            name=table_name,
            row_count=row_count,
            columns=columns,
            indexes=list(indexes_by_name.values()),
            triggers=triggers_by_table.get((schema_name, table_name), []),
        ))

    conn.close()
    return tables


def extract_views(connection_string: str) -> list[View]:
    conn = get_connection(connection_string)
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


def extract_procedures(connection_string: str) -> list[StoredProcedure]:
    conn = get_connection(connection_string)
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