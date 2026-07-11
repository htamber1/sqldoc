"""Small cross-driver helpers shared by the health and quality analysis paths.

Different drivers return different row objects: pyodbc rows and psycopg2
NamedTuple rows use attribute access (`row.col`), while mysql-connector dict
cursors and sqlite3.Row use key access (`row["col"]`). `cell()` reads a named
column from any of them so the analysis SQL can stay driver-agnostic.
"""


def cell(row, name):
    """Read column `name` from a row regardless of driver row type."""
    try:
        return row[name]                 # dict cursor, sqlite3.Row
    except (KeyError, IndexError, TypeError):
        return getattr(row, name)        # pyodbc, NamedTuple
