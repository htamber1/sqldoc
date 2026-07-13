"""SQLite adapter.

Uses the Python standard-library `sqlite3` driver (no extra dependency) and
SQLite's introspection surface: `PRAGMA table_info` / `foreign_key_list` /
`index_list` / `index_info` for tables, and `sqlite_master` for views and
triggers. Rows are read through `sqlite3.Row` (`row["col"]`).

SQLite has no schemas (everything reports under the logical schema `main`), no
stored procedures (`extract_procedures` returns `[]`), and does not expose CHECK
constraints or generated-column expressions through PRAGMA, so those are left
empty. Row counts are exact `COUNT(*)` (SQLite keeps no row estimate).

The connection string may be a bare file path (`./app.db`), a `sqlite://` URL,
or `file:` URI form.
"""
import re
import sqlite3

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities,
    Table, Column, Index, Trigger, UniqueConstraint,
    View, StoredProcedure,
)

_SCHEMA = "main"   # SQLite's default (only) schema name


def _db_path(connection_string: str) -> str:
    cs = connection_string or ""
    if cs.startswith("sqlite:///"):
        return cs[len("sqlite:///"):]
    if cs.startswith("sqlite://"):
        return cs[len("sqlite://"):]
    if cs.startswith("file:"):
        return cs[len("file:"):]
    return cs


def _trigger_events(sql: str) -> list[str]:
    s = (sql or "").upper()
    return [e for e in ("INSERT", "UPDATE", "DELETE") if e in s]


class SqliteAdapter(DatabaseAdapter):
    dialect = "sqlite"
    display_name = "SQLite"
    # Aggregate profiling works (standard SQL); no DMV analogue for health, and
    # no access-grant catalog for the comply audit.
    capabilities = Capabilities(quality=True, health=False, access_audit=False)

    @staticmethod
    def _default_connect(connection_string: str):
        conn = sqlite3.connect(_db_path(connection_string))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        # SQLite has only a file path; the database argument carries it.
        return database

    def _quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    # --- tables ------------------------------------------------------------

    def extract_metadata(self) -> list[Table]:
        conn = self.connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        table_names = [r["name"] for r in cursor.fetchall()]

        triggers_by_table = self._triggers(cursor)

        tables = []
        for name in table_names:
            columns = self._columns(cursor, name)
            indexes, uniques = self._indexes(cursor, name)
            cursor.execute(f"SELECT COUNT(*) AS n FROM {self._quote(name)}")  # nosec B608 - reviewed: only int-cast counts and dialect-quoted catalog identifiers interpolated, never raw user input (see SECURITY.md)
            row_count = int(cursor.fetchone()["n"] or 0)
            tables.append(Table(
                schema=_SCHEMA,
                name=name,
                row_count=row_count,
                columns=columns,
                indexes=indexes,
                triggers=triggers_by_table.get(name, []),
                check_constraints=[],   # not exposed via PRAGMA
                unique_constraints=uniques,
            ))
        conn.close()
        return tables

    def _columns(self, cursor, table_name) -> list[Column]:
        # foreign_key_list: from-column -> (referenced table, referenced column, actions)
        cursor.execute(f"PRAGMA foreign_key_list({self._quote(table_name)})")
        fk_by_col = {}
        for r in cursor.fetchall():
            fk_by_col[r["from"]] = (r["table"], r["to"], r["on_delete"], r["on_update"])

        cursor.execute(f"PRAGMA table_info({self._quote(table_name)})")
        columns = []
        for r in cursor.fetchall():
            fk = fk_by_col.get(r["name"])
            columns.append(Column(
                name=r["name"],
                data_type=(r["type"] or "").lower() or "blob",
                max_length=None,
                is_nullable=(int(r["notnull"]) == 0),
                is_primary_key=(int(r["pk"]) > 0),
                is_foreign_key=fk is not None,
                references_table=fk[0] if fk else None,
                references_column=fk[1] if fk else None,
                default_definition=str(r["dflt_value"]) if r["dflt_value"] is not None else None,
                fk_on_delete=fk[2] if fk else None,
                fk_on_update=fk[3] if fk else None,
            ))
        return columns

    def _indexes(self, cursor, table_name):
        cursor.execute(f"PRAGMA index_list({self._quote(table_name)})")
        index_rows = cursor.fetchall()
        indexes, uniques = [], []
        for ir in index_rows:
            iname = ir["name"]
            origin = ir["origin"]          # 'c' user index, 'u' UNIQUE, 'pk' primary key
            is_unique = int(ir["unique"]) == 1
            cursor.execute(f"PRAGMA index_info({self._quote(iname)})")
            cols = [c["name"] for c in cursor.fetchall() if c["name"] is not None]
            indexes.append(Index(
                name=iname,
                type_desc="INDEX",
                is_unique=is_unique,
                is_primary_key=(origin == "pk"),
                key_columns=cols,
            ))
            if origin == "u" and cols:
                uniques.append(UniqueConstraint(name=iname, columns=cols))
        return indexes, uniques

    def _triggers(self, cursor) -> dict:
        cursor.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
        )
        out = {}
        for r in cursor.fetchall():
            sql = r["sql"] or ""
            out.setdefault(r["tbl_name"], []).append(Trigger(
                name=r["name"],
                is_instead_of=bool(re.search(r"\bINSTEAD\s+OF\b", sql, re.IGNORECASE)),
                is_disabled=False,
                events=_trigger_events(sql),
                definition=sql or None,
            ))
        return out

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        conn = self.connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name")
        views_raw = cursor.fetchall()

        views = []
        for r in views_raw:
            cursor.execute(f"PRAGMA table_info({self._quote(r['name'])})")
            columns = [
                Column(
                    name=c["name"],
                    data_type=(c["type"] or "").lower() or "blob",
                    max_length=None,
                    is_nullable=(int(c["notnull"]) == 0),
                    is_primary_key=False,
                    is_foreign_key=False,
                    references_table=None,
                    references_column=None,
                )
                for c in cursor.fetchall()
            ]
            views.append(View(
                schema=_SCHEMA,
                name=r["name"],
                columns=columns,
                definition=r["sql"] or None,
            ))
        conn.close()
        return views

    # --- procedures --------------------------------------------------------

    def extract_procedures(self) -> list[StoredProcedure]:
        # SQLite has no stored procedures.
        return []
