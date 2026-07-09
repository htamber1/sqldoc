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

@dataclass
class Table:
    schema: str
    name: str
    row_count: int
    columns: list[Column] = field(default_factory=list)
    description: Optional[str] = None

def get_connection(server: str, database: str, username: str, password: str):
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        f"TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str)

def extract_metadata(server: str, database: str, username: str, password: str) -> list[Table]:
    conn = get_connection(server, database, username, password)
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
                ep.value AS description
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
                description=str(row.description) if row.description else None
            ))

        tables.append(Table(
            schema=schema_name,
            name=table_name,
            row_count=row_count,
            columns=columns
        ))

    conn.close()
    return tables