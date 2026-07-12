"""Wait-statistics analysis across dialects.

"What is the server waiting on?" — the single most useful performance question.
Each engine exposes waits differently, so this normalises them into five
categories (IO / Lock / Memory / CPU / Network) with a consistent report shape,
and can ask an LLM to explain the top waits in plain English and suggest fixes.

* **SQL Server** — cumulative ``sys.dm_os_wait_stats`` (benign waits filtered).
* **PostgreSQL** — a point-in-time snapshot from ``pg_stat_activity``
  (``wait_event_type`` / ``wait_event``) plus ungranted ``pg_locks``.
* **MySQL** — cumulative ``performance_schema.events_waits_summary_global_by_event_name``.

Metadata/statistics only — never table row data.
"""
from dataclasses import dataclass, field

import sqldoc.ai as ai
from sqldoc.dbutil import cell

WAIT_DIALECTS = {"sqlserver", "azuresql", "azure_managed_instance", "postgres", "mysql"}
CATEGORIES = ["IO", "Lock", "Memory", "CPU", "Network", "Other"]


def _s(v) -> str:
    return "" if v is None else str(v)


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return round(float(v or 0), 1)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class WaitStat:
    wait_type: str
    category: str
    wait_time_ms: float = 0.0
    waiting_tasks: int = 0
    percent: float = 0.0


@dataclass
class WaitReport:
    dialect: str = ""
    supported: bool = True
    snapshot: bool = False              # True for PG (point-in-time, not cumulative)
    unit: str = "ms"                    # or "sessions" for PG snapshot
    waits: list = field(default_factory=list)
    total_wait_ms: float = 0.0
    category_totals: dict = field(default_factory=dict)
    ai_explanation: str = ""
    errors: list = field(default_factory=list)


# --- categorisation --------------------------------------------------------

def _categorize_sqlserver(wait_type: str) -> str:
    w = wait_type.upper()
    if w.startswith(("PAGEIOLATCH", "WRITELOG", "IO_COMPLETION", "ASYNC_IO",
                     "BACKUPIO", "DISKIO", "LOGBUFFER")):
        return "IO"
    if w.startswith("LCK_"):
        return "Lock"
    if w.startswith(("RESOURCE_SEMAPHORE", "CMEMTHREAD", "MEMORY", "PAGELATCH")):
        return "Memory"
    if w.startswith(("SOS_SCHEDULER_YIELD", "CXPACKET", "CXCONSUMER", "THREADPOOL", "SOS_WORK")):
        return "CPU"
    if w.startswith(("ASYNC_NETWORK_IO", "NET_WAITFOR_PACKET", "DBMIRROR_SEND", "NETWORK")):
        return "Network"
    return "Other"


def _categorize_postgres(wait_event_type: str) -> str:
    t = (wait_event_type or "").lower()
    return {
        "io": "IO", "lock": "Lock", "lwlock": "Lock", "bufferpin": "Memory",
        "ipc": "CPU", "client": "Network", "timeout": "Other",
        "activity": "Other", "extension": "Other",
    }.get(t, "Other")


def _categorize_mysql(event_name: str) -> str:
    n = (event_name or "").lower()
    if n.startswith("wait/io/"):
        return "IO"
    if n.startswith("wait/lock/"):
        return "Lock"
    if n.startswith(("wait/synch/mutex", "wait/synch/rwlock", "wait/synch/sxlock", "wait/synch/prlock")):
        return "CPU"
    if n.startswith("wait/synch/cond"):
        return "Memory"
    return "Other"


# SQL Server benign waits to exclude (idle/background noise).
_SQLSERVER_IGNORE = (
    "SLEEP_TASK", "BROKER_TASK_STOP", "BROKER_TO_FLUSH", "BROKER_EVENTHANDLER",
    "LAZYWRITER_SLEEP", "SQLTRACE_BUFFER_FLUSH", "SQLTRACE_INCREMENTAL_FLUSH_SLEEP",
    "WAITFOR", "REQUEST_FOR_DEADLOCK_SEARCH", "XE_TIMER_EVENT", "XE_DISPATCHER_WAIT",
    "LOGMGR_QUEUE", "CHECKPOINT_QUEUE", "DIRTY_PAGE_POLL", "HADR_FILESTREAM_IOMGR_IOCOMPLETION",
    "DISPATCHER_QUEUE_SEMAPHORE", "FT_IFTS_SCHEDULER_IDLE_WAIT", "CLR_AUTO_EVENT",
    "CLR_MANUAL_EVENT", "SP_SERVER_DIAGNOSTICS_SLEEP", "QDS_PERSIST_TASK_MAIN_LOOP_SLEEP",
    "QDS_ASYNC_QUEUE", "QDS_SHUTDOWN_QUEUE", "HADR_WORK_QUEUE", "HADR_LOGCAPTURE_WAIT",
    "HADR_CLUSAPI_CALL", "PREEMPTIVE_XE_GETTARGETSTATE", "BROKER_RECEIVE_WAITFOR",
    "ONDEMAND_TASK_QUEUE", "DBMIRROR_DBM_EVENT", "DBMIRROR_EVENTS_QUEUE",
    "DBMIRRORING_CMD", "SLEEP_SYSTEMTASK", "SQLTRACE_WAIT_ENTRIES",
    # Background / benign waits that dominate an otherwise idle instance.
    "SOS_WORK_DISPATCHER", "PWAIT_EXTENSIBILITY_CLEANUP_TASK",
    "PWAIT_ALL_COMPONENTS_INITIALIZED", "PARALLEL_REDO_DRAIN_WORKER",
    "PARALLEL_REDO_LOG_CACHE", "PARALLEL_REDO_WORKER_WAIT_WORK",
    "VDI_CLIENT_OTHER", "PREEMPTIVE_OS_QUERYREGISTRY", "PREEMPTIVE_OS_GETPROCADDRESS",
    "PREEMPTIVE_OS_AUTHENTICATIONOPS", "PREEMPTIVE_OS_CRYPTOPS",
    "STARTUP_DEPENDENCY_MANAGER", "SERVER_IDLE_CHECK", "HADR_TIMER_TASK",
)


def _collect_sqlserver(cursor, top: int) -> WaitReport:
    report = WaitReport(dialect="sqlserver", unit="ms")
    ignore_list = ", ".join(f"'{w}'" for w in _SQLSERVER_IGNORE)
    cursor.execute(f"""
        SELECT TOP ({int(top)})
            wait_type,
            wait_time_ms,
            waiting_tasks_count
        FROM sys.dm_os_wait_stats
        WHERE wait_type NOT IN ({ignore_list})
          AND wait_time_ms > 0
        ORDER BY wait_time_ms DESC
    """)
    rows = cursor.fetchall()
    total = sum(_f(cell(r, "wait_time_ms")) for r in rows) or 1.0
    for r in rows:
        ms = _f(cell(r, "wait_time_ms"))
        wt = _s(cell(r, "wait_type"))
        cat = _categorize_sqlserver(wt)
        report.waits.append(WaitStat(wait_type=wt, category=cat, wait_time_ms=ms,
                                     waiting_tasks=_i(cell(r, "waiting_tasks_count")),
                                     percent=round(100.0 * ms / total, 1)))
        report.category_totals[cat] = report.category_totals.get(cat, 0.0) + ms
    report.total_wait_ms = round(total, 1)
    return report


def _collect_postgres(cursor, top: int) -> WaitReport:
    report = WaitReport(dialect="postgres", snapshot=True, unit="sessions")
    cursor.execute("""
        SELECT wait_event_type, wait_event, COUNT(*) AS n
        FROM pg_stat_activity
        WHERE wait_event_type IS NOT NULL AND pid <> pg_backend_pid()
        GROUP BY wait_event_type, wait_event
        ORDER BY n DESC
    """)
    rows = cursor.fetchall()
    counts = []
    for r in rows:
        wtype = _s(cell(r, "wait_event_type"))
        wevent = _s(cell(r, "wait_event"))
        counts.append((f"{wtype}:{wevent}", _categorize_postgres(wtype), _i(cell(r, "n"))))

    # Ungranted locks are the clearest lock-wait signal in PostgreSQL.
    try:
        cursor.execute("SELECT COUNT(*) AS blocked FROM pg_locks WHERE NOT granted")
        lrows = cursor.fetchall()
        blocked = _i(cell(lrows[0], "blocked")) if lrows else 0
        if blocked:
            counts.append(("Lock:ungranted", "Lock", blocked))
    except Exception as e:
        report.errors.append(("pg_locks", f"{type(e).__name__}: {e}"))

    counts.sort(key=lambda c: -c[2])
    counts = counts[:top]
    total = sum(c[2] for c in counts) or 1
    for name, cat, n in counts:
        report.waits.append(WaitStat(wait_type=name, category=cat, waiting_tasks=n,
                                     percent=round(100.0 * n / total, 1)))
        report.category_totals[cat] = report.category_totals.get(cat, 0.0) + n
    report.total_wait_ms = float(total)
    return report


def _collect_mysql(cursor, top: int) -> WaitReport:
    report = WaitReport(dialect="mysql", unit="ms")
    cursor.execute(f"""
        SELECT event_name, count_star, sum_timer_wait
        FROM performance_schema.events_waits_summary_global_by_event_name
        WHERE sum_timer_wait > 0 AND event_name <> 'idle'
        ORDER BY sum_timer_wait DESC
        LIMIT {int(top)}
    """)
    rows = cursor.fetchall()
    # sum_timer_wait is in picoseconds; convert to milliseconds.
    parsed = [(_s(cell(r, "event_name")),
               _f(cell(r, "sum_timer_wait")) / 1e9,
               _i(cell(r, "count_star"))) for r in rows]
    total = sum(ms for _, ms, _ in parsed) or 1.0
    for name, ms, cnt in parsed:
        cat = _categorize_mysql(name)
        report.waits.append(WaitStat(wait_type=name, category=cat, wait_time_ms=round(ms, 1),
                                     waiting_tasks=cnt, percent=round(100.0 * ms / total, 1)))
        report.category_totals[cat] = report.category_totals.get(cat, 0.0) + ms
    report.total_wait_ms = round(total, 1)
    return report


# --- dispatch --------------------------------------------------------------

def collect_waits(adapter, top: int = 15) -> WaitReport:
    dialect = getattr(adapter, "dialect", "sqlserver")
    if dialect not in WAIT_DIALECTS:
        return WaitReport(dialect=dialect, supported=False,
                          errors=[("Unsupported", f"Wait analysis is not implemented for {dialect}.")])
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        if dialect in ("sqlserver", "azuresql", "azure_managed_instance"):
            return _collect_sqlserver(cursor, top)
        if dialect == "postgres":
            return _collect_postgres(cursor, top)
        return _collect_mysql(cursor, top)
    finally:
        conn.close()


# --- AI explanation --------------------------------------------------------

def _ai_call(prompt, mode, model):
    return ai.dispatch(prompt, mode, model, max_tokens=700).strip()


def explain_waits(report: WaitReport, mode: str = "local", model: str = None) -> str:
    """Ask the LLM to explain the top waits and suggest fixes. Metadata only —
    only wait type names + percentages are sent, never any data."""
    if not report.waits:
        return ""
    top = report.waits[:8]
    lines = [f"- {w.wait_type} ({w.category}): {w.percent}% of total"
             + (f", {w.waiting_tasks} tasks" if w.waiting_tasks else "") for w in top]
    unit = "point-in-time session counts" if report.snapshot else "cumulative wait time"
    prompt = (
        f"You are a {report.dialect} performance expert. These are the top wait "
        f"statistics ({unit}) on a database server:\n" + "\n".join(lines) +
        "\n\nIn plain English, explain what the server is mostly waiting on, the "
        "likely root cause, and 2-4 concrete things to investigate or fix. Be "
        "specific to the wait categories shown. Keep it under 200 words.")
    return _ai_call(prompt, mode, model).strip()


def summarize(report: WaitReport) -> dict:
    cats = report.category_totals
    total = sum(cats.values()) or 1
    top_category = max(cats, key=cats.get) if cats else "None"
    return {
        "waits": len(report.waits),
        "total_wait_ms": report.total_wait_ms,
        "top_category": top_category,
        "snapshot": report.snapshot,
        "category_percent": {c: round(100.0 * cats.get(c, 0) / total, 1) for c in CATEGORIES if cats.get(c)},
        "has_ai": bool(report.ai_explanation),
    }
