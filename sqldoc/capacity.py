"""Capacity planning from the agent's historical metrics.

The agent records a size/disk/fragmentation snapshot on every poll. This module
(a) collects that snapshot per dialect (used by the poller) and (b) projects the
trends — days until disk full, days until the database hits its max size, the
fastest-growing tables with 30/60/90-day projections, and the fragmentation
trend — using a simple linear rate over the recorded history.

Projection is pure (operates on the stored history); collection reads only size
catalog metadata. SQL Server gives the richest signal (disk + max size +
fragmentation); PostgreSQL/MySQL provide database + table sizes.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqldoc.dbutil import cell

CAPACITY_DIALECTS = {"sqlserver", "azuresql", "azure_managed_instance", "postgres", "mysql"}


def _f(v):
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# --- collection (per poll) -------------------------------------------------

def collect_capacity_snapshot(adapter) -> dict:
    """Return {database_size_mb, disk_free_mb, disk_total_mb, max_size_mb,
    fragmentation_avg, top_tables:[(obj,size_mb,rows)]} for the adapter's dialect.
    Missing pieces are None/omitted; never raises for a single failed sub-query."""
    dialect = getattr(adapter, "dialect", "sqlserver")
    snap = {"database_size_mb": None, "disk_free_mb": None, "disk_total_mb": None,
            "max_size_mb": None, "fragmentation_avg": None, "top_tables": []}
    if dialect not in CAPACITY_DIALECTS:
        return snap
    try:
        conn = adapter.connect()
        cursor = adapter.cursor(conn)
    except Exception:
        return snap

    def sub(fn):
        try:
            fn()
        except Exception:
            pass

    try:
        if dialect in ("sqlserver", "azuresql", "azure_managed_instance"):
            _capacity_sqlserver(cursor, snap, sub)
        elif dialect == "postgres":
            _capacity_postgres(cursor, snap, sub)
        else:
            _capacity_mysql(cursor, snap, sub)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return snap


def _capacity_sqlserver(cursor, snap, sub):
    def size_disk():
        cursor.execute("""
            -- CAPACITY_SIZE
            SELECT
                SUM(CASE WHEN f.type = 0 THEN CAST(f.size AS bigint) END) * 8 / 1024.0 AS db_mb,
                SUM(CASE WHEN f.type = 0 AND f.max_size > 0 THEN CAST(f.max_size AS bigint) END) * 8 / 1024.0 AS max_mb,
                MAX(vs.available_bytes) / 1048576.0 AS disk_free_mb,
                MAX(vs.total_bytes) / 1048576.0 AS disk_total_mb
            FROM sys.database_files f
            CROSS APPLY sys.dm_os_volume_stats(DB_ID(), f.file_id) vs
        """)
        rows = cursor.fetchall()
        if rows:
            r = rows[0]
            snap["database_size_mb"] = _f(cell(r, "db_mb"))
            snap["max_size_mb"] = _f(cell(r, "max_mb"))
            snap["disk_free_mb"] = _f(cell(r, "disk_free_mb"))
            snap["disk_total_mb"] = _f(cell(r, "disk_total_mb"))

    def frag():
        cursor.execute("""
            -- CAPACITY_FRAG
            SELECT AVG(avg_fragmentation_in_percent) AS frag
            FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED')
            WHERE page_count > 100
        """)
        rows = cursor.fetchall()
        if rows:
            snap["fragmentation_avg"] = _f(cell(rows[0], "frag"))

    def tables():
        cursor.execute("""
            -- CAPACITY_TABLES
            SELECT TOP 20 s.name + '.' + t.name AS obj,
                   SUM(ps.reserved_page_count) * 8 / 1024.0 AS size_mb,
                   MAX(p.rows) AS row_count
            FROM sys.dm_db_partition_stats ps
            INNER JOIN sys.tables t ON ps.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            GROUP BY s.name, t.name
            ORDER BY SUM(ps.reserved_page_count) DESC
        """)
        snap["top_tables"] = [(cell(r, "obj"), _f(cell(r, "size_mb")) or 0.0, _i(cell(r, "row_count")))
                              for r in cursor.fetchall()]

    sub(size_disk)
    sub(frag)
    sub(tables)


def _capacity_postgres(cursor, snap, sub):
    def size():
        cursor.execute("SELECT pg_database_size(current_database()) / 1048576.0 AS db_mb  -- CAPACITY_SIZE")
        rows = cursor.fetchall()
        if rows:
            snap["database_size_mb"] = _f(cell(rows[0], "db_mb"))

    def tables():
        cursor.execute("""
            -- CAPACITY_TABLES
            SELECT schemaname || '.' || relname AS obj,
                   pg_total_relation_size(relid) / 1048576.0 AS size_mb,
                   n_live_tup AS row_count
            FROM pg_stat_user_tables
            ORDER BY pg_total_relation_size(relid) DESC
            LIMIT 20
        """)
        snap["top_tables"] = [(cell(r, "obj"), _f(cell(r, "size_mb")) or 0.0, _i(cell(r, "row_count")))
                              for r in cursor.fetchall()]

    sub(size)
    sub(tables)


def _capacity_mysql(cursor, snap, sub):
    def tables():
        cursor.execute("""
            -- CAPACITY_TABLES
            SELECT CONCAT(table_schema, '.', table_name) AS obj,
                   (data_length + index_length) / 1048576.0 AS size_mb,
                   table_rows AS row_count
            FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'
            ORDER BY (data_length + index_length) DESC
            LIMIT 20
        """)
        top = [(cell(r, "obj"), _f(cell(r, "size_mb")) or 0.0, _i(cell(r, "row_count")))
               for r in cursor.fetchall()]
        snap["top_tables"] = top
        snap["database_size_mb"] = round(sum(t[1] for t in top), 2)

    sub(tables)


# --- projection (from stored history) --------------------------------------

@dataclass
class Projection:
    metric: str
    current: float = 0.0
    rate_per_day: float = 0.0
    days_until_limit: float = None      # None = not applicable / never
    limit: float = None
    unit: str = "MB"
    history: list = field(default_factory=list)   # (at, value)


@dataclass
class TableGrowth:
    obj: str
    current_mb: float
    rate_mb_per_day: float
    size_30d: float
    size_60d: float
    size_90d: float


@dataclass
class CapacityReport:
    database: str
    points: int = 0
    span_days: float = 0.0
    sufficient: bool = False
    disk: Projection = None
    db_size: Projection = None
    fragmentation: Projection = None
    table_growth: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def _parse_at(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "").split("+")[0])
    except (TypeError, ValueError):
        return None


def _rate(series):
    """series: list of (datetime, value). Returns (rate_per_day, span_days) from
    first to last non-None value."""
    pts = [(t, v) for t, v in series if t is not None and v is not None]
    if len(pts) < 2:
        return 0.0, 0.0
    (t0, v0), (t1, v1) = pts[0], pts[-1]
    span = (t1 - t0).total_seconds() / 86400.0
    if span <= 0:
        return 0.0, 0.0
    return (v1 - v0) / span, span


def project_capacity(database, metrics_history, table_history) -> CapacityReport:
    report = CapacityReport(database=database)
    # Only rows that carry a capacity snapshot.
    rows = [r for r in metrics_history if r.get("database_size_mb") is not None]
    report.points = len(rows)
    if len(rows) < 2:
        report.notes.append("Not enough history to project — the agent needs at least two "
                            "polling cycles with capacity metrics.")
        return report
    report.sufficient = True
    times = [_parse_at(r.get("at")) for r in rows]
    span = 0.0
    if times[0] and times[-1]:
        span = (times[-1] - times[0]).total_seconds() / 86400.0
    report.span_days = round(span, 3)

    # Disk-free projection.
    disk_series = list(zip(times, [r.get("disk_free_mb") for r in rows]))
    if any(v is not None for _, v in disk_series):
        rate, _sp = _rate(disk_series)
        cur = next((v for _, v in reversed(disk_series) if v is not None), 0.0)
        p = Projection("disk_free", current=round(cur, 1), rate_per_day=round(rate, 2),
                       history=[(str(r.get("at")), r.get("disk_free_mb")) for r in rows])
        if rate < 0:
            p.days_until_limit = round(cur / (-rate), 1)
        report.disk = p

    # Database-size projection vs max size.
    size_series = list(zip(times, [r.get("database_size_mb") for r in rows]))
    rate, _sp = _rate(size_series)
    cur = rows[-1].get("database_size_mb") or 0.0
    limit = rows[-1].get("max_size_mb")
    p = Projection("database_size", current=round(cur, 1), rate_per_day=round(rate, 2), limit=limit,
                   history=[(str(r.get("at")), r.get("database_size_mb")) for r in rows])
    if limit and rate > 0 and limit > cur:
        p.days_until_limit = round((limit - cur) / rate, 1)
    report.db_size = p

    # Fragmentation trend.
    frag_series = list(zip(times, [r.get("fragmentation_avg") for r in rows]))
    if any(v is not None for _, v in frag_series):
        rate, _sp = _rate(frag_series)
        cur = next((v for _, v in reversed(frag_series) if v is not None), 0.0)
        report.fragmentation = Projection(
            "fragmentation", current=round(cur, 1), rate_per_day=round(rate, 3), unit="%",
            history=[(str(r.get("at")), r.get("fragmentation_avg")) for r in rows])

    # Fastest-growing tables.
    by_obj = {}
    for row in table_history:
        by_obj.setdefault(row["obj"], []).append((_parse_at(row.get("at")), row.get("size_mb")))
    growth = []
    for obj, series in by_obj.items():
        rate, sp = _rate(series)
        cur = next((v for _, v in reversed(series) if v is not None), 0.0) or 0.0
        if rate <= 0:
            continue
        growth.append(TableGrowth(
            obj=obj, current_mb=round(cur, 1), rate_mb_per_day=round(rate, 3),
            size_30d=round(cur + rate * 30, 1), size_60d=round(cur + rate * 60, 1),
            size_90d=round(cur + rate * 90, 1)))
    growth.sort(key=lambda g: -g.rate_mb_per_day)
    report.table_growth = growth[:15]
    return report


def summarize(report: CapacityReport) -> dict:
    return {
        "points": report.points,
        "span_days": report.span_days,
        "sufficient": report.sufficient,
        "disk_days_until_full": report.disk.days_until_limit if report.disk else None,
        "db_days_until_max": report.db_size.days_until_limit if report.db_size else None,
        "growing_tables": len(report.table_growth),
        "fragmentation_now": report.fragmentation.current if report.fragmentation else None,
    }
