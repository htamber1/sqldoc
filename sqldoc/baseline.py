"""Performance baseline capture + anomaly detection across dialects.

``sqldoc baseline --capture`` records a performance snapshot — connection count,
wait-category totals, top query average times, and (SQL Server) SQL Agent job
average durations — to a JSON file. A later ``sqldoc baseline`` run captures the
current snapshot and compares it to the saved baseline, flagging any metric or
query that has regressed by more than a configurable percentage.

Metrics/statistics only — never table row data. Works on SQL Server,
PostgreSQL, and MySQL.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime

from sqldoc.dbutil import cell

BASELINE_DIALECTS = {"sqlserver", "azuresql", "postgres", "mysql"}


def _f(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


@dataclass
class Baseline:
    dialect: str = ""
    captured_at: str = ""
    metrics: dict = field(default_factory=dict)    # name -> value (higher = worse)
    queries: list = field(default_factory=list)    # [{id, avg_ms, text}]


@dataclass
class Anomaly:
    metric: str
    baseline: float
    current: float
    change_pct: float
    kind: str = "metric"        # metric / query
    detail: str = ""


@dataclass
class ComparisonReport:
    dialect: str = ""
    baseline_at: str = ""
    current_at: str = ""
    threshold_pct: float = 25.0
    metrics_compared: int = 0
    anomalies: list = field(default_factory=list)


# --- capture ---------------------------------------------------------------

def _capture_connections(cursor, dialect) -> int:
    if dialect in ("sqlserver", "azuresql"):
        cursor.execute("SELECT COUNT(*) AS n FROM sys.dm_exec_sessions WHERE is_user_process = 1  -- BASELINE_CONN")
    elif dialect == "postgres":
        cursor.execute("SELECT COUNT(*) AS n FROM pg_stat_activity WHERE pid <> pg_backend_pid()  -- BASELINE_CONN")
    else:
        cursor.execute("SELECT COUNT(*) AS n FROM information_schema.processlist  -- BASELINE_CONN")
    rows = cursor.fetchall()
    return _i(cell(rows[0], "n")) if rows else 0


def _capture_queries(cursor, dialect, top) -> list:
    if dialect in ("sqlserver", "azuresql"):
        cursor.execute(f"""
            -- BASELINE_QUERIES
            SELECT TOP ({int(top)})
                CONVERT(varchar(34), qs.query_hash, 1) AS qid,
                (qs.total_elapsed_time / qs.execution_count) / 1000.0 AS avg_ms,
                SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
                    ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                      ELSE qs.statement_end_offset END - qs.statement_start_offset)/2)+1) AS query_text
            FROM sys.dm_exec_query_stats qs
            CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
            ORDER BY qs.total_elapsed_time DESC
        """)
    elif dialect == "postgres":
        cursor.execute(f"""
            SELECT queryid AS qid, mean_exec_time AS avg_ms, query AS query_text  -- BASELINE_QUERIES
            FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT {int(top)}
        """)
    else:
        cursor.execute(f"""
            SELECT DIGEST AS qid, AVG_TIMER_WAIT / 1e9 AS avg_ms, DIGEST_TEXT AS query_text  -- BASELINE_QUERIES
            FROM performance_schema.events_statements_summary_by_digest
            WHERE DIGEST_TEXT IS NOT NULL ORDER BY SUM_TIMER_WAIT DESC LIMIT {int(top)}
        """)
    out = []
    for r in cursor.fetchall():
        out.append({"id": str(cell(r, "qid")), "avg_ms": _f(cell(r, "avg_ms")),
                    "text": _collapse_ws(str(cell(r, "query_text") or ""))[:300]})
    return out


def capture_baseline(adapter, top: int = 15) -> Baseline:
    """Capture a performance snapshot for the adapter's dialect."""
    dialect = getattr(adapter, "dialect", "sqlserver")
    b = Baseline(dialect=dialect, captured_at=datetime.now().isoformat(timespec="seconds"))

    # Wait-category totals (its own connection).
    try:
        from sqldoc.waits import collect_waits
        w = collect_waits(adapter)
        b.metrics["total_wait_ms"] = round(w.total_wait_ms, 1)
        for cat, ms in w.category_totals.items():
            b.metrics[f"wait_{cat}_ms"] = round(ms, 1)
    except Exception:
        pass

    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        b.metrics["connections"] = _capture_connections(cursor, dialect)
        b.queries = _capture_queries(cursor, dialect, top)
        if b.queries:
            b.metrics["slowest_query_ms"] = max(q["avg_ms"] for q in b.queries)
        if dialect in ("sqlserver", "azuresql"):
            try:
                from sqldoc.server import collect_agent_jobs
                for j in collect_agent_jobs(cursor):
                    if j.avg_duration_seconds:
                        b.metrics[f"job_{j.name}_avg_s"] = j.avg_duration_seconds
            except Exception:
                pass
    finally:
        conn.close()
    return b


# --- persistence -----------------------------------------------------------

def to_dict(b: Baseline) -> dict:
    return {"schema_version": 1, "type": "sqldoc-baseline", **asdict(b)}


def from_dict(d: dict) -> Baseline:
    return Baseline(dialect=d.get("dialect", ""), captured_at=d.get("captured_at", ""),
                    metrics=dict(d.get("metrics") or {}), queries=list(d.get("queries") or []))


# --- comparison ------------------------------------------------------------

def compare_baseline(baseline: Baseline, current: Baseline, threshold_pct: float = 25.0) -> ComparisonReport:
    report = ComparisonReport(dialect=current.dialect, baseline_at=baseline.captured_at,
                              current_at=current.captured_at, threshold_pct=threshold_pct)
    factor = 1 + threshold_pct / 100.0
    floor = 1.0     # ignore near-zero metrics (noise)

    for name, cur_v in current.metrics.items():
        base_v = baseline.metrics.get(name)
        if base_v is None:
            continue
        report.metrics_compared += 1
        if base_v >= floor and cur_v > base_v * factor:
            report.anomalies.append(Anomaly(
                metric=name, baseline=round(base_v, 1), current=round(cur_v, 1),
                change_pct=round(100.0 * (cur_v - base_v) / base_v, 1), kind="metric"))

    base_q = {q["id"]: q for q in baseline.queries}
    for q in current.queries:
        bq = base_q.get(q["id"])
        if bq and bq["avg_ms"] >= floor and q["avg_ms"] > bq["avg_ms"] * factor:
            report.anomalies.append(Anomaly(
                metric=f"query {q['id']}", baseline=round(bq["avg_ms"], 1),
                current=round(q["avg_ms"], 1),
                change_pct=round(100.0 * (q["avg_ms"] - bq["avg_ms"]) / bq["avg_ms"], 1),
                kind="query", detail=q.get("text", "")))

    report.anomalies.sort(key=lambda a: -a.change_pct)
    return report


def summarize(report: ComparisonReport) -> dict:
    return {
        "anomalies": len(report.anomalies),
        "metric_regressions": sum(1 for a in report.anomalies if a.kind == "metric"),
        "query_regressions": sum(1 for a in report.anomalies if a.kind == "query"),
        "metrics_compared": report.metrics_compared,
        "worst_change_pct": max((a.change_pct for a in report.anomalies), default=0.0),
    }
