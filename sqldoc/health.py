"""Database health analysis via SQL Server dynamic management views (DMVs).

Four independent checks, each reading only server/DB statistics (never table row
data):

* **Slow queries**    — `sys.dm_exec_query_stats` + `sys.dm_exec_sql_text`:
                        the costliest cached statements by average elapsed time.
* **Dead tables**     — `sys.dm_db_index_usage_stats`: tables with writes but no
                        reads since the stats last reset (candidate dead weight).
* **Missing indexes** — `sys.dm_db_missing_index_details` + group stats: indexes
                        the optimizer wished existed, ranked by benefit.
* **Index fragmentation** — `sys.dm_db_index_physical_stats`: indexes fragmented
                        past a threshold and large enough to matter.

Every check runs in its own try/except: the DMVs need `VIEW SERVER STATE` (and
`dm_exec_sql_text` needs elevated rights), so a permission failure degrades that
one section to an error message rather than aborting the whole report. The DMV
statistics are transient (reset on restart / `DBCC`), which the report notes.
"""
from dataclasses import dataclass, field

from sqldoc.dbutil import cell
from sqldoc.extractor import get_connection   # retained for back-compat imports/tests


@dataclass
class SlowQuery:
    query_text: str
    execution_count: int
    avg_elapsed_ms: float
    total_elapsed_ms: float
    avg_logical_reads: int
    last_execution: str


@dataclass
class DeadTable:
    schema: str
    table: str
    row_count: int
    user_seeks: int
    user_scans: int
    user_lookups: int
    user_updates: int
    last_read: str

    @property
    def reads(self) -> int:
        return self.user_seeks + self.user_scans + self.user_lookups


@dataclass
class MissingIndex:
    schema: str
    table: str
    equality_columns: str
    inequality_columns: str
    included_columns: str
    user_seeks: int
    avg_user_impact: float
    improvement_measure: float

    def create_statement(self) -> str:
        """A ready-to-review CREATE INDEX for this recommendation."""
        key_parts = [c.strip() for c in
                     (self.equality_columns or "").strip("[]").split(",") if c.strip()]
        key_parts += [c.strip() for c in
                      (self.inequality_columns or "").strip("[]").split(",") if c.strip()]
        keys = ", ".join(key_parts) or "/* columns */"
        incl = ""
        if self.included_columns:
            incl = f" INCLUDE ({self.included_columns})"
        cols_slug = "_".join(p.strip("[] ") for p in key_parts) or "cols"
        return (f"CREATE INDEX IX_{self.table}_{cols_slug} "
                f"ON [{self.schema}].[{self.table}] ({keys}){incl};")


@dataclass
class FragmentedIndex:
    schema: str
    table: str
    index_name: str
    avg_fragmentation_percent: float
    page_count: int

    @property
    def recommendation(self) -> str:
        # SQL Server's long-standing rule of thumb.
        return "REBUILD" if self.avg_fragmentation_percent >= 30 else "REORGANIZE"


@dataclass
class HealthReport:
    database: str
    slow_queries: list = field(default_factory=list)
    dead_tables: list = field(default_factory=list)
    missing_indexes: list = field(default_factory=list)
    fragmented_indexes: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # (section, message) for degraded checks


def _s(v) -> str:
    return "" if v is None else str(v)


def _collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


def collect_slow_queries(cursor, top: int) -> list:
    cursor.execute(f"""
        SELECT TOP ({int(top)})
            qs.execution_count,
            qs.total_elapsed_time / 1000.0 AS total_elapsed_ms,
            (qs.total_elapsed_time / qs.execution_count) / 1000.0 AS avg_elapsed_ms,
            qs.total_logical_reads / qs.execution_count AS avg_logical_reads,
            qs.last_execution_time,
            SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
                ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                  ELSE qs.statement_end_offset END - qs.statement_start_offset)/2)+1) AS query_text
        FROM sys.dm_exec_query_stats qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
        ORDER BY avg_elapsed_ms DESC
    """)
    out = []
    for r in cursor.fetchall():
        out.append(SlowQuery(
            query_text=_collapse_ws(_s(r.query_text))[:500],
            execution_count=int(r.execution_count or 0),
            avg_elapsed_ms=round(float(r.avg_elapsed_ms or 0), 2),
            total_elapsed_ms=round(float(r.total_elapsed_ms or 0), 2),
            avg_logical_reads=int(r.avg_logical_reads or 0),
            last_execution=_s(r.last_execution_time),
        ))
    return out


def collect_dead_tables(cursor) -> list:
    cursor.execute("""
        SELECT
            s.name AS schema_name,
            t.name AS table_name,
            p.rows AS row_count,
            ISNULL(us.user_seeks, 0)   AS user_seeks,
            ISNULL(us.user_scans, 0)   AS user_scans,
            ISNULL(us.user_lookups, 0) AS user_lookups,
            ISNULL(us.user_updates, 0) AS user_updates,
            us.last_user_scan
        FROM sys.tables t
        INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
        INNER JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
        LEFT JOIN sys.dm_db_index_usage_stats us
            ON us.object_id = t.object_id AND us.database_id = DB_ID() AND us.index_id IN (0, 1)
        ORDER BY p.rows DESC
    """)
    out = []
    for r in cursor.fetchall():
        dt = DeadTable(
            schema=r.schema_name, table=r.table_name, row_count=int(r.row_count or 0),
            user_seeks=int(r.user_seeks or 0), user_scans=int(r.user_scans or 0),
            user_lookups=int(r.user_lookups or 0), user_updates=int(r.user_updates or 0),
            last_read=_s(r.last_user_scan),
        )
        # "Dead" = never read since stats reset. Flag only tables with rows,
        # so we don't nag about empty scaffolding.
        if dt.reads == 0 and dt.row_count > 0:
            out.append(dt)
    return out


def collect_missing_indexes(cursor, top: int) -> list:
    cursor.execute(f"""
        SELECT TOP ({int(top)})
            s.name AS schema_name,
            t.name AS table_name,
            mid.equality_columns,
            mid.inequality_columns,
            mid.included_columns,
            migs.user_seeks,
            migs.avg_user_impact,
            migs.avg_total_user_cost * migs.avg_user_impact * (migs.user_seeks + migs.user_scans) AS improvement_measure
        FROM sys.dm_db_missing_index_details mid
        INNER JOIN sys.tables t ON mid.object_id = t.object_id
        INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
        INNER JOIN sys.dm_db_missing_index_groups mig ON mid.index_handle = mig.index_handle
        INNER JOIN sys.dm_db_missing_index_group_stats migs ON mig.index_group_handle = migs.group_handle
        WHERE mid.database_id = DB_ID()
        ORDER BY improvement_measure DESC
    """)
    out = []
    for r in cursor.fetchall():
        out.append(MissingIndex(
            schema=r.schema_name, table=r.table_name,
            equality_columns=_s(r.equality_columns),
            inequality_columns=_s(r.inequality_columns),
            included_columns=_s(r.included_columns),
            user_seeks=int(r.user_seeks or 0),
            avg_user_impact=round(float(r.avg_user_impact or 0), 1),
            improvement_measure=round(float(r.improvement_measure or 0), 1),
        ))
    return out


def collect_fragmented_indexes(cursor, min_fragmentation: float, min_pages: int) -> list:
    cursor.execute(f"""
        SELECT
            s.name AS schema_name,
            t.name AS table_name,
            i.name AS index_name,
            ips.avg_fragmentation_in_percent,
            ips.page_count
        FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED') ips
        INNER JOIN sys.tables t ON ips.object_id = t.object_id
        INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
        INNER JOIN sys.indexes i ON ips.object_id = i.object_id AND ips.index_id = i.index_id
        WHERE ips.avg_fragmentation_in_percent >= {float(min_fragmentation)}
          AND ips.page_count >= {int(min_pages)}
          AND i.name IS NOT NULL
        ORDER BY ips.avg_fragmentation_in_percent DESC
    """)
    out = []
    for r in cursor.fetchall():
        out.append(FragmentedIndex(
            schema=r.schema_name, table=r.table_name, index_name=r.index_name,
            avg_fragmentation_percent=round(float(r.avg_fragmentation_in_percent or 0), 1),
            page_count=int(r.page_count or 0),
        ))
    return out


# --- PostgreSQL checks -----------------------------------------------------
# pg_stat_user_tables gives per-table read/write counters; pg_stat_statements
# (an extension) gives statement timings. Missing-index and fragmentation
# advice have no native analogue, so those sections degrade to a note.

def collect_pg_dead_tables(cursor) -> list:
    cursor.execute("""
        SELECT schemaname AS schema_name,
               relname AS table_name,
               n_live_tup AS row_count,
               COALESCE(idx_scan, 0) AS user_seeks,
               COALESCE(seq_scan, 0) AS user_scans,
               COALESCE(n_tup_ins, 0) + COALESCE(n_tup_upd, 0) + COALESCE(n_tup_del, 0) AS user_updates,
               last_analyze
        FROM pg_stat_user_tables
        ORDER BY n_live_tup DESC
    """)
    out = []
    for r in cursor.fetchall():
        dt = DeadTable(
            schema=cell(r, "schema_name"), table=cell(r, "table_name"),
            row_count=int(cell(r, "row_count") or 0),
            user_seeks=int(cell(r, "user_seeks") or 0),
            user_scans=int(cell(r, "user_scans") or 0),
            user_lookups=0, user_updates=int(cell(r, "user_updates") or 0),
            last_read=_s(cell(r, "last_analyze")),
        )
        if dt.reads == 0 and dt.row_count > 0:
            out.append(dt)
    return out


def collect_pg_slow_queries(cursor, top: int) -> list:
    # Requires the pg_stat_statements extension; if absent this raises and the
    # section degrades. Columns are the PG 13+ names (total_exec_time / mean_exec_time).
    cursor.execute(f"""
        SELECT query AS query_text,
               calls AS execution_count,
               total_exec_time AS total_elapsed_ms,
               mean_exec_time AS avg_elapsed_ms,
               rows AS avg_logical_reads,
               '' AS last_execution
        FROM pg_stat_statements
        ORDER BY mean_exec_time DESC
        LIMIT {int(top)}
    """)
    out = []
    for r in cursor.fetchall():
        out.append(SlowQuery(
            query_text=_collapse_ws(_s(cell(r, "query_text")))[:500],
            execution_count=int(cell(r, "execution_count") or 0),
            avg_elapsed_ms=round(float(cell(r, "avg_elapsed_ms") or 0), 2),
            total_elapsed_ms=round(float(cell(r, "total_elapsed_ms") or 0), 2),
            avg_logical_reads=int(cell(r, "avg_logical_reads") or 0),
            last_execution=_s(cell(r, "last_execution")),
        ))
    return out


# --- MySQL checks ----------------------------------------------------------
# performance_schema surfaces per-table I/O counters and per-statement digests.
# Timer columns are in picoseconds (÷1e9 → ms). Missing-index / fragmentation
# advice degrade to a note.

def collect_mysql_dead_tables(cursor) -> list:
    cursor.execute("""
        SELECT t.OBJECT_SCHEMA AS schema_name,
               t.OBJECT_NAME AS table_name,
               COALESCE(it.TABLE_ROWS, 0) AS row_count,
               t.COUNT_READ AS user_scans,
               t.COUNT_WRITE AS user_updates
        FROM performance_schema.table_io_waits_summary_by_table t
        JOIN information_schema.tables it
          ON it.table_schema = t.OBJECT_SCHEMA AND it.table_name = t.OBJECT_NAME
        WHERE t.OBJECT_SCHEMA = DATABASE() AND t.OBJECT_TYPE = 'TABLE'
        ORDER BY it.TABLE_ROWS DESC
    """)
    out = []
    for r in cursor.fetchall():
        dt = DeadTable(
            schema=cell(r, "schema_name"), table=cell(r, "table_name"),
            row_count=int(cell(r, "row_count") or 0),
            user_seeks=0, user_scans=int(cell(r, "user_scans") or 0),
            user_lookups=0, user_updates=int(cell(r, "user_updates") or 0),
            last_read="",
        )
        if dt.reads == 0 and dt.row_count > 0:
            out.append(dt)
    return out


def collect_mysql_slow_queries(cursor, top: int) -> list:
    cursor.execute(f"""
        SELECT DIGEST_TEXT AS query_text,
               COUNT_STAR AS execution_count,
               SUM_TIMER_WAIT / 1e9 AS total_elapsed_ms,
               AVG_TIMER_WAIT / 1e9 AS avg_elapsed_ms,
               SUM_ROWS_EXAMINED / GREATEST(COUNT_STAR, 1) AS avg_logical_reads,
               LAST_SEEN AS last_execution
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST_TEXT IS NOT NULL
        ORDER BY AVG_TIMER_WAIT DESC
        LIMIT {int(top)}
    """)
    out = []
    for r in cursor.fetchall():
        out.append(SlowQuery(
            query_text=_collapse_ws(_s(cell(r, "query_text")))[:500],
            execution_count=int(cell(r, "execution_count") or 0),
            avg_elapsed_ms=round(float(cell(r, "avg_elapsed_ms") or 0), 2),
            total_elapsed_ms=round(float(cell(r, "total_elapsed_ms") or 0), 2),
            avg_logical_reads=int(cell(r, "avg_logical_reads") or 0),
            last_execution=_s(cell(r, "last_execution")),
        ))
    return out


# --- orchestration ---------------------------------------------------------

_NOT_AVAILABLE = "NotAvailable: no equivalent system view on this dialect"


def _checks_for(dialect, cursor, top, min_fragmentation, min_pages):
    """Return the [(label, fn, attr)] check list for a dialect. Sections with no
    analogue are represented by a fn that raises, so they degrade uniformly."""
    def unavailable():
        raise RuntimeError(_NOT_AVAILABLE)

    if dialect in ("sqlserver", "azuresql"):
        return [
            ("Slow queries", lambda: collect_slow_queries(cursor, top), "slow_queries"),
            ("Dead tables", lambda: collect_dead_tables(cursor), "dead_tables"),
            ("Missing indexes", lambda: collect_missing_indexes(cursor, top), "missing_indexes"),
            ("Index fragmentation",
             lambda: collect_fragmented_indexes(cursor, min_fragmentation, min_pages),
             "fragmented_indexes"),
        ]
    if dialect == "postgres":
        return [
            ("Slow queries (pg_stat_statements)", lambda: collect_pg_slow_queries(cursor, top), "slow_queries"),
            ("Dead tables", lambda: collect_pg_dead_tables(cursor), "dead_tables"),
            ("Missing indexes", unavailable, "missing_indexes"),
            ("Index fragmentation", unavailable, "fragmented_indexes"),
        ]
    if dialect == "mysql":
        return [
            ("Slow queries (performance_schema)", lambda: collect_mysql_slow_queries(cursor, top), "slow_queries"),
            ("Dead tables", lambda: collect_mysql_dead_tables(cursor), "dead_tables"),
            ("Missing indexes", unavailable, "missing_indexes"),
            ("Index fragmentation", unavailable, "fragmented_indexes"),
        ]
    return []


def collect_health(adapter, top: int = 20,
                   min_fragmentation: float = 10.0, min_pages: int = 100,
                   schemas: list = None) -> HealthReport:
    """Run the health checks appropriate to the adapter's dialect. Each check is
    isolated so a missing permission/extension (or a section with no analogue on
    this dialect) degrades to a note in `report.errors` instead of failing the
    whole run. `schemas`, if given, filters the table-scoped checks."""
    report = HealthReport(database="")
    dialect = getattr(adapter, "dialect", "sqlserver")
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        for label, fn, attr in _checks_for(dialect, cursor, top, min_fragmentation, min_pages):
            try:
                setattr(report, attr, fn())
            except Exception as e:
                report.errors.append((label, f"{type(e).__name__}: {e}"))
    finally:
        conn.close()

    if schemas:
        allow = set(schemas)
        report.dead_tables = [d for d in report.dead_tables if d.schema in allow]
        report.missing_indexes = [m for m in report.missing_indexes if m.schema in allow]
        report.fragmented_indexes = [f for f in report.fragmented_indexes if f.schema in allow]
    return report


def summarize(report: HealthReport) -> dict:
    return {
        "slow_queries": len(report.slow_queries),
        "dead_tables": len(report.dead_tables),
        "missing_indexes": len(report.missing_indexes),
        "fragmented_indexes": len(report.fragmented_indexes),
        "issues": (len(report.slow_queries) + len(report.dead_tables)
                   + len(report.missing_indexes) + len(report.fragmented_indexes)),
        "degraded": len(report.errors),
    }
