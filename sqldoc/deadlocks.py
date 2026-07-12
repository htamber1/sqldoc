"""Deadlock analysis across dialects.

Surfaces deadlocks (and current blocking chains) and turns them into a wait-for
graph, with an optional AI explanation of the cause and fix:

* **SQL Server** — parses ``xml_deadlock_report`` events from the always-on
  ``system_health`` extended-events session: victim, participating processes
  (with their SQL), and the resource wait-for edges — a full deadlock graph.
* **PostgreSQL** — cumulative ``pg_stat_database.deadlocks`` count plus the
  *current* blocking tree from ``pg_stat_activity`` + ``pg_blocking_pids()``
  (blocked → blocker edges).
* **MySQL** — deadlock error count from
  ``performance_schema.events_errors_summary_global_by_error`` (ER_LOCK_DEADLOCK).

The SQL Server path parses the deadlock graph (which includes the offending SQL);
the AI explanation receives that graph. PG/MySQL are count/blocking-based.
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import sqldoc.ai as ai
from sqldoc.dbutil import cell

DEADLOCK_DIALECTS = {"sqlserver", "azuresql", "azure_managed_instance", "postgres", "mysql"}


def _s(v) -> str:
    return "" if v is None else str(v)


def _collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


@dataclass
class DeadlockProcess:
    id: str
    spid: str = ""
    database: str = ""
    login: str = ""
    host: str = ""
    lock_mode: str = ""
    wait_resource: str = ""
    query: str = ""
    is_victim: bool = False


@dataclass
class DeadlockEvent:
    kind: str = "graph"          # "graph" (SQL Server) / "current-blocking" (PG)
    timestamp: str = ""
    victim_id: str = ""
    processes: list = field(default_factory=list)     # DeadlockProcess
    edges: list = field(default_factory=list)         # (waiter_id, owner_id)
    resources: list = field(default_factory=list)     # objectname strings
    description: str = ""


@dataclass
class DeadlockReport:
    dialect: str = ""
    supported: bool = True
    events: list = field(default_factory=list)        # DeadlockEvent
    total_count: int = 0                              # cumulative deadlocks (PG/MySQL)
    mechanism: str = ""
    ai_explanation: str = ""
    notes: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# --- SQL Server (parse the system_health deadlock graphs) ------------------

def parse_deadlock_xml(target_xml: str) -> list:
    """Parse ring-buffer target XML into DeadlockEvents (one per deadlock graph)."""
    events = []
    if not target_xml:
        return events
    root = ET.fromstring(target_xml)
    for ev in root.iter("event"):
        if ev.get("name") != "xml_deadlock_report":
            continue
        timestamp = ev.get("timestamp", "")
        deadlock = ev.find(".//deadlock")
        if deadlock is None:
            continue
        victim_el = deadlock.find("victim-list/victimProcess")
        victim_id = victim_el.get("id") if victim_el is not None else ""

        procs = {}
        for p in deadlock.findall("process-list/process"):
            pid = p.get("id", "")
            inputbuf = p.find("inputbuf")
            query = _collapse_ws(inputbuf.text if inputbuf is not None and inputbuf.text else "")
            procs[pid] = DeadlockProcess(
                id=pid, spid=p.get("spid", ""), database=p.get("currentdb", ""),
                login=p.get("loginname", ""), host=p.get("hostname", ""),
                lock_mode=p.get("lockMode", ""), wait_resource=p.get("waitresource", ""),
                query=query[:400], is_victim=(pid == victim_id))

        edges, resources = [], []
        for res in list(deadlock.find("resource-list") or []):
            obj = res.get("objectname") or res.tag
            resources.append(obj)
            owners = [o.get("id") for o in res.findall("owner-list/owner")]
            waiters = [w.get("id") for w in res.findall("waiter-list/waiter")]
            for w in waiters:
                for o in owners:
                    if w and o and w != o:
                        edges.append((w, o))

        events.append(DeadlockEvent(
            kind="graph", timestamp=timestamp, victim_id=victim_id,
            processes=list(procs.values()), edges=list(set(edges)), resources=resources))
    return events


def _collect_sqlserver(cursor) -> DeadlockReport:
    report = DeadlockReport(dialect="sqlserver", mechanism="system_health deadlock graphs")
    cursor.execute("""
        SELECT CAST(st.target_data AS xml) AS target_xml
        FROM sys.dm_xe_session_targets st
        INNER JOIN sys.dm_xe_sessions s ON s.address = st.event_session_address
        WHERE s.name = 'system_health' AND st.target_name = 'ring_buffer'
          AND CAST(st.target_data AS nvarchar(max)) LIKE '%xml_deadlock_report%'
    """)
    rows = cursor.fetchall()
    for r in rows:
        try:
            report.events.extend(parse_deadlock_xml(_s(cell(r, "target_xml"))))
        except ET.ParseError as e:
            report.errors.append(("Parse deadlock XML", str(e)))
    report.total_count = len(report.events)
    if not report.events:
        report.notes.append("No deadlocks found in the system_health session ring buffer "
                            "(it is size-limited, so older deadlocks may have rolled off).")
    return report


# --- PostgreSQL (deadlock count + current blocking tree) -------------------

def _collect_postgres(cursor) -> DeadlockReport:
    report = DeadlockReport(dialect="postgres", mechanism="pg_stat_database + current blocking")
    cursor.execute("SELECT COALESCE(SUM(deadlocks), 0) AS total_deadlocks FROM pg_stat_database")
    rows = cursor.fetchall()
    report.total_count = int(cell(rows[0], "total_deadlocks") or 0) if rows else 0

    cursor.execute("""
        SELECT blocked.pid AS blocked_pid, blocked.usename AS blocked_user, blocked.query AS blocked_query,
               blocking.pid AS blocking_pid, blocking.usename AS blocking_user, blocking.query AS blocking_query
        FROM pg_stat_activity blocked
        JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(pid) ON true
        JOIN pg_stat_activity blocking ON blocking.pid = bp.pid
    """)
    for r in cursor.fetchall():
        blocked_pid = _s(cell(r, "blocked_pid"))
        blocking_pid = _s(cell(r, "blocking_pid"))
        report.events.append(DeadlockEvent(
            kind="current-blocking",
            victim_id=blocked_pid,
            processes=[
                DeadlockProcess(id=blocked_pid, spid=blocked_pid, login=_s(cell(r, "blocked_user")),
                                query=_collapse_ws(_s(cell(r, "blocked_query")))[:400], is_victim=True),
                DeadlockProcess(id=blocking_pid, spid=blocking_pid, login=_s(cell(r, "blocking_user")),
                                query=_collapse_ws(_s(cell(r, "blocking_query")))[:400]),
            ],
            edges=[(blocked_pid, blocking_pid)],
            description="Currently blocked (not necessarily a deadlock)."))
    if report.total_count == 0 and not report.events:
        report.notes.append("No deadlocks recorded and nothing is currently blocked.")
    else:
        report.notes.append(f"{report.total_count} deadlock(s) recorded cumulatively "
                            f"(pg_stat_database). Current blocking chains shown below, if any.")
    return report


# --- MySQL (deadlock error count) ------------------------------------------

def _collect_mysql(cursor) -> DeadlockReport:
    report = DeadlockReport(dialect="mysql", mechanism="performance_schema error counts")
    cursor.execute("""
        SELECT SUM_ERROR_COUNT AS n
        FROM performance_schema.events_errors_summary_global_by_error
        WHERE ERROR_NAME = 'ER_LOCK_DEADLOCK'
    """)
    rows = cursor.fetchall()
    report.total_count = int(cell(rows[0], "n") or 0) if rows else 0
    report.notes.append(
        f"{report.total_count} deadlock error(s) (ER_LOCK_DEADLOCK) recorded since startup. "
        f"MySQL does not retain deadlock graphs in the catalog — run SHOW ENGINE INNODB STATUS "
        f"for the latest detected deadlock.")
    return report


# --- dispatch --------------------------------------------------------------

def collect_deadlocks(adapter) -> DeadlockReport:
    dialect = getattr(adapter, "dialect", "sqlserver")
    if dialect not in DEADLOCK_DIALECTS:
        return DeadlockReport(dialect=dialect, supported=False,
                              errors=[("Unsupported", f"Deadlock analysis is not implemented for {dialect}.")])
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        if dialect in ("sqlserver", "azuresql", "azure_managed_instance"):
            return _collect_sqlserver(cursor)
        if dialect == "postgres":
            return _collect_postgres(cursor)
        return _collect_mysql(cursor)
    finally:
        conn.close()


# --- AI explanation --------------------------------------------------------

def _ai_call(prompt, mode, model):
    return ai.dispatch(prompt, mode, model, max_tokens=700).strip()


def explain_deadlock(report: DeadlockReport, mode: str = "local", model: str = None) -> str:
    """Ask the LLM to explain the most recent deadlock's cause and how to fix it.
    NOTE: this sends the deadlock's SQL statements to the model."""
    if not report.events:
        return ""
    ev = report.events[0]
    lines = []
    for p in ev.processes:
        role = "VICTIM" if p.is_victim else "process"
        lines.append(f"- {role} spid {p.spid} (login {p.login}) holding/waiting {p.lock_mode} "
                     f"on {p.wait_resource or 'a resource'}: {p.query or '(query unavailable)'}")
    prompt = (
        f"You are a {report.dialect} deadlock expert. Here is a deadlock's processes and their SQL:\n"
        + "\n".join(lines) +
        "\n\nExplain in plain English why this deadlock happened (the cyclic lock "
        "dependency), which statement/order caused it, and 2-4 concrete ways to prevent "
        "it (e.g. consistent access order, covering indexes, shorter transactions, lower "
        "isolation). Keep it under 200 words.")
    return _ai_call(prompt, mode, model).strip()


def summarize(report: DeadlockReport) -> dict:
    graph_events = [e for e in report.events if e.kind == "graph"]
    return {
        "deadlocks": len(graph_events) if graph_events else report.total_count,
        "total_count": report.total_count,
        "graph_events": len(graph_events),
        "current_blocking": sum(1 for e in report.events if e.kind == "current-blocking"),
        "has_ai": bool(report.ai_explanation),
        "mechanism": report.mechanism,
    }
