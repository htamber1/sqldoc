"""IBM Db2 adapter.

Reads tables/columns/views/procedures/indexes from the ``SYSCAT`` catalog via the
``ibm-db`` driver (its DBAPI layer, ``ibm_db_dbi``), and adds Db2-specific
operational metadata:

* **Tablespace** configuration (``SYSCAT.TABLESPACES``); each table's tablespace
  is folded into its description.
* **Buffer pool** hit ratios (``MON_GET_BUFFERPOOL``).
* **Lock-wait** analysis (``SYSIBMADM.MON_LOCKWAITS`` / ``MON_GET_LOCKWAITS``).

Db2 rows come back as tuples, so results are zipped with ``cursor.description``
into dict rows. The driver is an *optional* dependency imported lazily; a missing
driver raises a clear ``pip install sqldoc[db2]`` error. Detected from a
``db2://`` or ``ibm-db2://`` scheme.

NOTE: mock-tested only — not run against a live Db2 instance.
"""
from urllib.parse import unquote, urlparse

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities, Table, Column, Index, View, StoredProcedure,
)


def _s(v):
    return "" if v is None else str(v).rstrip()


def _split_colnames(colnames: str) -> list:
    """Db2 index COLNAMES look like '+ORDER_ID+CUSTOMER_ID-STATUS'."""
    out, cur = [], ""
    for ch in _s(colnames):
        if ch in "+-":
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


class Db2Adapter(DatabaseAdapter):
    dialect = "db2"
    display_name = "IBM Db2"
    capabilities = Capabilities(quality=False, health=False, access_audit=False,
                                triggers=False, computed_columns=False, check_constraints=False)

    @staticmethod
    def _default_connect(connection_string: str):
        try:
            import ibm_db_dbi
        except ImportError as e:
            raise ImportError(
                "IBM Db2 support requires the 'ibm-db' driver, which is not installed. "
                "Install it with:  pip install sqldoc[db2]  (or:  pip install ibm-db)."
            ) from e
        return ibm_db_dbi.connect(Db2Adapter._dsn(connection_string), "", "")

    @staticmethod
    def _dsn(connection_string: str) -> str:
        u = urlparse(connection_string.replace("ibm-db2://", "db2://"))
        db = (u.path or "").lstrip("/") or "sample"
        parts = [f"DATABASE={db}", f"HOSTNAME={u.hostname or 'localhost'}",
                 f"PORT={u.port or 50000}", "PROTOCOL=TCPIP"]
        if u.username:
            parts.append(f"UID={unquote(u.username)}")
        if u.password:
            parts.append(f"PWD={unquote(u.password)}")
        return ";".join(parts) + ";"

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        return f"db2://{username}:{password}@{server}/{database}"

    def _rows(self, cursor, sql, params=None):
        cursor.execute(sql, params or [])
        cols = [d[0].lower() for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]

    # --- tables ------------------------------------------------------------

    def extract_metadata(self) -> list[Table]:
        conn = self.connect()
        cursor = conn.cursor()
        tables_raw = self._rows(cursor, """
            SELECT TABSCHEMA, TABNAME, CARD, TBSPACE, REMARKS
            FROM SYSCAT.TABLES
            WHERE TYPE = 'T' AND TABSCHEMA NOT LIKE 'SYS%'
            ORDER BY TABSCHEMA, TABNAME
        """)
        idx_by_table = self._indexes(cursor)

        tables = []
        for row in tables_raw:
            schema_name, table_name = _s(row["tabschema"]), _s(row["tabname"])
            columns = self._columns(cursor, schema_name, table_name)
            desc = _s(row.get("remarks")) or None
            tbspace = _s(row.get("tbspace"))
            if tbspace:
                desc = (f"[Db2 tablespace: {tbspace}] " + (desc or "")).strip()
            tables.append(Table(
                schema=schema_name, name=table_name,
                row_count=max(int(row.get("card") or 0), 0),
                columns=columns,
                indexes=idx_by_table.get((schema_name, table_name), []),
                triggers=[], check_constraints=[], unique_constraints=[],
                description=desc,
            ))
        conn.close()
        return tables

    def _columns(self, cursor, schema_name, table_name):
        rows = self._rows(cursor, """
            SELECT COLNAME, TYPENAME, LENGTH, NULLS, KEYSEQ, REMARKS
            FROM SYSCAT.COLUMNS
            WHERE TABSCHEMA = ? AND TABNAME = ?
            ORDER BY COLNO
        """, [schema_name, table_name])
        columns = []
        for r in rows:
            columns.append(Column(
                name=_s(r["colname"]), data_type=_s(r["typename"]),
                max_length=(int(r["length"]) if r.get("length") is not None else None),
                is_nullable=(_s(r.get("nulls")).upper() == "Y"),
                is_primary_key=bool(r.get("keyseq")),
                is_foreign_key=False, references_table=None, references_column=None,
                description=(_s(r.get("remarks")) or None),
            ))
        return columns

    def _indexes(self, cursor) -> dict:
        rows = self._rows(cursor, """
            SELECT TABSCHEMA, TABNAME, INDNAME, UNIQUERULE, COLNAMES
            FROM SYSCAT.INDEXES
            WHERE TABSCHEMA NOT LIKE 'SYS%'
        """)
        out = {}
        for r in rows:
            rule = _s(r.get("uniquerule")).upper()
            idx = Index(
                name=_s(r["indname"]),
                type_desc="CLUSTERED" if rule == "P" else "NONCLUSTERED",
                is_unique=rule in ("P", "U"),
                is_primary_key=rule == "P",
                key_columns=_split_colnames(r.get("colnames")),
                included_columns=[],
            )
            out.setdefault((_s(r["tabschema"]), _s(r["tabname"])), []).append(idx)
        return out

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        conn = self.connect()
        cursor = conn.cursor()
        rows = self._rows(cursor, """
            SELECT VIEWSCHEMA, VIEWNAME, TEXT
            FROM SYSCAT.VIEWS
            WHERE VIEWSCHEMA NOT LIKE 'SYS%'
            ORDER BY VIEWSCHEMA, VIEWNAME
        """)
        views = [View(schema=_s(r["viewschema"]), name=_s(r["viewname"]),
                      columns=[], definition=(_s(r.get("text")) or None)) for r in rows]
        conn.close()
        return views

    # --- procedures --------------------------------------------------------

    def extract_procedures(self) -> list[StoredProcedure]:
        conn = self.connect()
        cursor = conn.cursor()
        rows = self._rows(cursor, """
            SELECT ROUTINESCHEMA, ROUTINENAME, TEXT
            FROM SYSCAT.ROUTINES
            WHERE ROUTINETYPE = 'P' AND ROUTINESCHEMA NOT LIKE 'SYS%'
            ORDER BY ROUTINESCHEMA, ROUTINENAME
        """)
        procs = [StoredProcedure(schema=_s(r["routineschema"]), name=_s(r["routinename"]),
                                 parameters=[], definition=(_s(r.get("text")) or None)) for r in rows]
        conn.close()
        return procs

    # --- Db2-specific operational metadata ---------------------------------

    def db2_tablespaces(self) -> list:
        conn = self.connect()
        cursor = conn.cursor()
        out = []
        try:
            for r in self._rows(cursor, """
                SELECT TBSPACE, TBSPACETYPE, PAGESIZE, BUFFERPOOLID
                FROM SYSCAT.TABLESPACES ORDER BY TBSPACE
            """):
                out.append({"tablespace": _s(r["tbspace"]), "type": _s(r.get("tbspacetype")),
                            "page_size": int(r.get("pagesize") or 0),
                            "bufferpool_id": int(r.get("bufferpoolid") or 0)})
        except Exception:
            pass
        finally:
            conn.close()
        return out

    def db2_bufferpools(self) -> list:
        conn = self.connect()
        cursor = conn.cursor()
        out = []
        try:
            for r in self._rows(cursor, """
                SELECT BP_NAME, POOL_DATA_L_READS, POOL_DATA_P_READS
                FROM TABLE(MON_GET_BUFFERPOOL('', -2)) AS t
            """):
                logical = int(r.get("pool_data_l_reads") or 0)
                physical = int(r.get("pool_data_p_reads") or 0)
                hit = round(100.0 * (logical - physical) / logical, 1) if logical else None
                out.append({"bufferpool": _s(r["bp_name"]), "logical_reads": logical,
                            "physical_reads": physical, "hit_ratio_pct": hit})
        except Exception:
            pass
        finally:
            conn.close()
        return out

    def db2_lock_waits(self) -> list:
        conn = self.connect()
        cursor = conn.cursor()
        out = []
        try:
            for r in self._rows(cursor, """
                SELECT HLD_APPLICATION_HANDLE, REQ_APPLICATION_HANDLE,
                       LOCK_MODE, LOCK_OBJECT_TYPE, LOCK_WAIT_ELAPSED_TIME
                FROM SYSIBMADM.MON_LOCKWAITS
            """):
                out.append({"holder": _s(r.get("hld_application_handle")),
                            "waiter": _s(r.get("req_application_handle")),
                            "lock_mode": _s(r.get("lock_mode")),
                            "object_type": _s(r.get("lock_object_type")),
                            "wait_ms": int(r.get("lock_wait_elapsed_time") or 0)})
        except Exception:
            pass
        finally:
            conn.close()
        return out
