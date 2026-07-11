"""Oracle Database adapter.

Extracts metadata from the ``ALL_*`` data-dictionary views (`ALL_TABLES`,
`ALL_TAB_COLUMNS`, `ALL_CONSTRAINTS` / `ALL_CONS_COLUMNS`, `ALL_INDEXES` /
`ALL_IND_COLUMNS`, `ALL_TRIGGERS`, `ALL_VIEWS`, `ALL_PROCEDURES`,
`ALL_ARGUMENTS`), scoped to one schema (owner). The driver (``oracledb`` — the
modern successor to ``cx_Oracle``) is an *optional* dependency imported lazily
inside ``_default_connect``; a missing driver raises a clear error.

Rows are read via ``_rows`` which turns each cursor result into a dict keyed by
lower-cased column names, so the extraction code uses ``row["col"]`` uniformly.

Oracle has no ON UPDATE referential action (``fk_on_update`` is always None) and
auto-generates ``NOT NULL`` check constraints (filtered out). The owning schema
defaults to the connecting user; identifiers are upper-cased by Oracle.

NOTE: this adapter is **mock-tested only** — it has not run against a live Oracle
instance (that needs a licensed database). The SQL follows the documented
data-dictionary surface.

Connection string form: ``oracle://user:password@host:port/service_name``
"""
import re
from urllib.parse import unquote, urlparse

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities,
    Table, Column, Index, Trigger, CheckConstraint, UniqueConstraint,
    View, Parameter, StoredProcedure,
)

_NOT_NULL = re.compile(r'IS\s+NOT\s+NULL', re.IGNORECASE)


def _trigger_events(triggering_event: str) -> list[str]:
    s = (triggering_event or "").upper()
    return [e for e in ("INSERT", "UPDATE", "DELETE") if e in s]


class OracleAdapter(DatabaseAdapter):
    dialect = "oracle"
    display_name = "Oracle"
    # Metadata only for now; health/quality/comply are not ported.
    capabilities = Capabilities(quality=False, health=False, access_audit=False)

    def __init__(self, connection_string, connect=None):
        super().__init__(connection_string, connect=connect)
        u = urlparse(connection_string)
        self._owner = (unquote(u.username).upper() if u.username else "")

    @staticmethod
    def _default_connect(connection_string: str):
        try:
            import oracledb
        except ImportError as e:
            raise ImportError(
                "Oracle support requires the 'oracledb' driver, which is not "
                "installed. Install it with:  pip install sqldoc[oracle]  "
                "(or:  pip install oracledb)."
            ) from e
        u = urlparse(connection_string)
        service = (u.path or "").lstrip("/")
        dsn = f"{u.hostname or 'localhost'}:{u.port or 1521}/{service}"
        return oracledb.connect(
            user=unquote(u.username) if u.username else None,
            password=unquote(u.password) if u.password else None,
            dsn=dsn,
        )

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        # server carries host[:port]; database carries the service name.
        return f"oracle://{username}:{password}@{server}/{database}"

    def _rows(self, cursor, sql, params=None):
        cursor.execute(sql, params or {})
        cols = [d[0].lower() for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]

    # --- tables ------------------------------------------------------------

    def extract_metadata(self) -> list[Table]:
        conn = self.connect()
        cursor = conn.cursor()
        owner = {"owner": self._owner}

        tables_raw = self._rows(cursor, """
            SELECT table_name, num_rows
            FROM all_tables
            WHERE owner = :owner
            ORDER BY table_name
        """, owner)

        triggers_by_table = self._triggers(cursor)

        tables = []
        for row in tables_raw:
            name = row["table_name"]
            columns = self._columns(cursor, name)
            indexes, uniques = self._indexes_and_uniques(cursor, name, columns)
            checks = self._check_constraints(cursor, name)
            tables.append(Table(
                schema=self._owner,
                name=name,
                row_count=int(row["num_rows"] or 0),
                columns=columns,
                indexes=indexes,
                triggers=triggers_by_table.get(name, []),
                check_constraints=checks,
                unique_constraints=uniques,
            ))
        conn.close()
        return tables

    def _columns(self, cursor, table_name) -> list[Column]:
        params = {"owner": self._owner, "table_name": table_name}
        pk_cols = {r["column_name"] for r in self._rows(cursor, """
            SELECT cc.column_name
            FROM all_constraints c
            JOIN all_cons_columns cc
              ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
            WHERE c.owner = :owner AND c.table_name = :table_name AND c.constraint_type = 'P'
        """, params)}

        fk_by_col = {}
        for r in self._rows(cursor, """
            SELECT cc.column_name AS fk_column,
                   rcc.table_name AS ref_table,
                   rcc.column_name AS ref_column,
                   c.delete_rule
            FROM all_constraints c
            JOIN all_cons_columns cc
              ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
            JOIN all_cons_columns rcc
              ON rcc.owner = c.r_owner AND rcc.constraint_name = c.r_constraint_name
             AND rcc.position = cc.position
            WHERE c.owner = :owner AND c.table_name = :table_name AND c.constraint_type = 'R'
        """, params):
            fk_by_col[r["fk_column"]] = (r["ref_table"], r["ref_column"], r["delete_rule"])

        columns = []
        for r in self._rows(cursor, """
            SELECT column_name, data_type, data_length, nullable, data_default
            FROM all_tab_columns
            WHERE owner = :owner AND table_name = :table_name
            ORDER BY column_id
        """, params):
            fk = fk_by_col.get(r["column_name"])
            columns.append(Column(
                name=r["column_name"],
                data_type=(r["data_type"] or "").lower(),
                max_length=r["data_length"],
                is_nullable=(str(r["nullable"]).upper() == "Y"),
                is_primary_key=r["column_name"] in pk_cols,
                is_foreign_key=fk is not None,
                references_table=fk[0] if fk else None,
                references_column=fk[1] if fk else None,
                default_definition=(str(r["data_default"]).strip()
                                    if r["data_default"] is not None else None),
                fk_on_delete=fk[2] if fk else None,
                fk_on_update=None,   # Oracle has no ON UPDATE referential action
            ))
        return columns

    def _indexes_and_uniques(self, cursor, table_name, columns):
        params = {"owner": self._owner, "table_name": table_name}
        pk_cols = [c.name for c in columns if c.is_primary_key]

        by_name = {}
        for r in self._rows(cursor, """
            SELECT i.index_name, i.uniqueness, ic.column_name, ic.column_position
            FROM all_indexes i
            JOIN all_ind_columns ic
              ON ic.index_owner = i.owner AND ic.index_name = i.index_name
            WHERE i.table_owner = :owner AND i.table_name = :table_name
            ORDER BY i.index_name, ic.column_position
        """, params):
            idx = by_name.get(r["index_name"])
            if idx is None:
                idx = Index(name=r["index_name"], type_desc="INDEX",
                            is_unique=(str(r["uniqueness"]).upper() == "UNIQUE"),
                            is_primary_key=False)
                by_name[r["index_name"]] = idx
            idx.key_columns.append(r["column_name"])
        # An index whose columns are exactly the PK is the primary-key index.
        for idx in by_name.values():
            if pk_cols and idx.key_columns == pk_cols:
                idx.is_primary_key = True

        uniques = []
        u_by_name = {}
        for r in self._rows(cursor, """
            SELECT c.constraint_name, cc.column_name
            FROM all_constraints c
            JOIN all_cons_columns cc
              ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
            WHERE c.owner = :owner AND c.table_name = :table_name AND c.constraint_type = 'U'
            ORDER BY c.constraint_name, cc.position
        """, params):
            uq = u_by_name.get(r["constraint_name"])
            if uq is None:
                uq = UniqueConstraint(name=r["constraint_name"])
                u_by_name[r["constraint_name"]] = uq
                uniques.append(uq)
            uq.columns.append(r["column_name"])
        return list(by_name.values()), uniques

    def _check_constraints(self, cursor, table_name) -> list[CheckConstraint]:
        params = {"owner": self._owner, "table_name": table_name}
        out = []
        for r in self._rows(cursor, """
            SELECT constraint_name, search_condition
            FROM all_constraints
            WHERE owner = :owner AND table_name = :table_name AND constraint_type = 'C'
            ORDER BY constraint_name
        """, params):
            cond = r["search_condition"]
            cond = str(cond) if cond is not None else ""
            # Skip Oracle's implicit NOT NULL check constraints.
            if _NOT_NULL.search(cond):
                continue
            out.append(CheckConstraint(name=r["constraint_name"], definition=cond, column=None))
        return out

    def _triggers(self, cursor) -> dict:
        out = {}
        for r in self._rows(cursor, """
            SELECT trigger_name, table_name, triggering_event, trigger_type,
                   status, trigger_body
            FROM all_triggers
            WHERE table_owner = :owner AND table_name IS NOT NULL
            ORDER BY table_name, trigger_name
        """, {"owner": self._owner}):
            body = r["trigger_body"]
            out.setdefault(r["table_name"], []).append(Trigger(
                name=r["trigger_name"],
                is_instead_of=("INSTEAD OF" in str(r["trigger_type"] or "").upper()),
                is_disabled=(str(r["status"]).upper() == "DISABLED"),
                events=_trigger_events(r["triggering_event"]),
                definition=str(body) if body is not None else None,
            ))
        return out

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        conn = self.connect()
        cursor = conn.cursor()
        views_raw = self._rows(cursor, """
            SELECT view_name, text
            FROM all_views
            WHERE owner = :owner
            ORDER BY view_name
        """, {"owner": self._owner})

        views = []
        for row in views_raw:
            cols = self._rows(cursor, """
                SELECT column_name, data_type, data_length, nullable
                FROM all_tab_columns
                WHERE owner = :owner AND table_name = :table_name
                ORDER BY column_id
            """, {"owner": self._owner, "table_name": row["view_name"]})
            columns = [
                Column(
                    name=c["column_name"],
                    data_type=(c["data_type"] or "").lower(),
                    max_length=c["data_length"],
                    is_nullable=(str(c["nullable"]).upper() == "Y"),
                    is_primary_key=False,
                    is_foreign_key=False,
                    references_table=None,
                    references_column=None,
                )
                for c in cols
            ]
            text = row["text"]
            views.append(View(
                schema=self._owner,
                name=row["view_name"],
                columns=columns,
                definition=str(text) if text is not None else None,
            ))
        conn.close()
        return views

    # --- procedures + functions --------------------------------------------

    def extract_procedures(self) -> list[StoredProcedure]:
        conn = self.connect()
        cursor = conn.cursor()
        procs_raw = self._rows(cursor, """
            SELECT object_name, object_type
            FROM all_procedures
            WHERE owner = :owner
              AND object_type IN ('PROCEDURE', 'FUNCTION')
              AND procedure_name IS NULL
            ORDER BY object_name
        """, {"owner": self._owner})

        procedures = []
        for row in procs_raw:
            params = []
            for pr in self._rows(cursor, """
                SELECT argument_name, data_type, in_out
                FROM all_arguments
                WHERE owner = :owner AND object_name = :object_name
                  AND package_name IS NULL AND argument_name IS NOT NULL
                ORDER BY position
            """, {"owner": self._owner, "object_name": row["object_name"]}):
                params.append(Parameter(
                    name=pr["argument_name"],
                    data_type=(pr["data_type"] or "").lower(),
                    max_length=None,
                    is_output=("OUT" in str(pr["in_out"] or "").upper()),
                ))
            procedures.append(StoredProcedure(
                schema=self._owner,
                name=row["object_name"],
                parameters=params,
                definition=None,   # source is in ALL_SOURCE; not extracted here
                description=None,
            ))
        conn.close()
        return procedures
