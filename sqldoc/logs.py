"""SQL Server ERRORLOG reader via ``sys.xp_readerrorlog``.

Reads the SQL Server (or SQL Agent) error log directly from the instance, with
optional search / severity / time-window filtering, and automatically classifies
entries that match well-known critical patterns — corruption, deadlocks, memory
pressure, disk-full, and login failures — so the dangerous lines stand out.

``xp_readerrorlog`` is an undocumented-but-ubiquitous extended procedure; it
needs sysadmin (or the granted EXEC). Reads log text only — no table row data.
SQL Server only.
"""
import re
from dataclasses import dataclass, field

from sqldoc.dbutil import cell


def _s(v) -> str:
    return "" if v is None else str(v)


# Severity / error-number embedded in the log text, e.g.
# "Error: 823, Severity: 24, State: 2."
_SEVERITY_RE = re.compile(r"Severity:\s*(\d+)", re.IGNORECASE)
_ERRORNO_RE = re.compile(r"Error:\s*(\d+)", re.IGNORECASE)

# Critical categories → the patterns (message text or well-known error numbers)
# that identify them. Checked in order; the first match wins.
_CRITICAL_PATTERNS = [
    ("corruption", re.compile(
        r"\b(823|824|825)\b|consistency error|corrupt|torn page|checksum|CHECKDB", re.IGNORECASE)),
    ("deadlock", re.compile(r"deadlock|\b1205\b", re.IGNORECASE)),
    ("memory-pressure", re.compile(
        r"out of memory|memory pressure|failed to allocate|insufficient memory|\b701\b", re.IGNORECASE)),
    ("disk-full", re.compile(
        r"disk is full|insufficient disk|could not allocate space|no space|\b1105\b|\b9002\b", re.IGNORECASE)),
    ("login-failure", re.compile(r"login failed|\b18456\b", re.IGNORECASE)),
]


@dataclass
class LogEntry:
    log_date: str
    process_info: str
    text: str
    severity: int = None
    error_number: int = None
    critical: str = ""        # category slug, or "" if not critical


@dataclass
class LogReport:
    source: str = "SQL Server ERRORLOG"
    entries: list = field(default_factory=list)
    search: str = ""
    severity_filter: int = None
    last_hours: int = None
    errors: list = field(default_factory=list)


def _parse_int(regex, text):
    m = regex.search(text or "")
    return int(m.group(1)) if m else None


def classify_critical(text: str) -> str:
    for category, pattern in _CRITICAL_PATTERNS:
        if pattern.search(text or ""):
            return category
    return ""


def read_error_log(cursor, log_number: int = 0, search: str = None,
                   last_hours: int = None, log_type: int = 1) -> list:
    """Run xp_readerrorlog with optional server-side search + time window.

    log_type 1 = SQL Server error log, 2 = SQL Agent log.
    """
    if last_hours:
        # A DECLARE'd variable keeps the datetime out of the EXEC arg list
        # (EXEC arguments must be constants/variables, not expressions).
        cursor.execute(
            "DECLARE @start DATETIME = DATEADD(HOUR, ?, GETDATE()); "
            "EXEC sys.xp_readerrorlog ?, ?, ?, NULL, @start, NULL, N'DESC';",
            -int(last_hours), int(log_number), int(log_type), search)
    else:
        cursor.execute(
            "EXEC sys.xp_readerrorlog ?, ?, ?, NULL, NULL, NULL, N'DESC';",
            int(log_number), int(log_type), search)

    entries = []
    for r in cursor.fetchall():
        text = _s(cell(r, "Text"))
        entries.append(LogEntry(
            log_date=_s(cell(r, "LogDate")),
            process_info=_s(cell(r, "ProcessInfo")),
            text=text,
            severity=_parse_int(_SEVERITY_RE, text),
            error_number=_parse_int(_ERRORNO_RE, text),
            critical=classify_critical(text),
        ))
    return entries


def collect_logs(adapter, log_number: int = 0, search: str = None,
                 severity: int = None, last_hours: int = None) -> LogReport:
    """Read the error log and apply the severity filter. Each read is isolated so
    a permission failure degrades to a note rather than crashing."""
    report = LogReport(search=search or "", severity_filter=severity, last_hours=last_hours)
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        entries = read_error_log(cursor, log_number, search, last_hours)
    except Exception as e:
        report.errors.append(("Read ERRORLOG", f"{type(e).__name__}: {e}"))
        entries = []
    finally:
        conn.close()

    if severity is not None:
        # Keep only entries whose parsed severity meets the threshold.
        entries = [e for e in entries if e.severity is not None and e.severity >= severity]
    report.entries = entries
    return report


def summarize(report: LogReport) -> dict:
    entries = report.entries
    by_category = {}
    max_sev = 0
    for e in entries:
        if e.critical:
            by_category[e.critical] = by_category.get(e.critical, 0) + 1
        if e.severity:
            max_sev = max(max_sev, e.severity)
    return {
        "entries": len(entries),
        "critical": sum(1 for e in entries if e.critical),
        "by_category": by_category,
        "max_severity": max_sev,
        "high_severity": sum(1 for e in entries if e.severity and e.severity >= 17),
        "degraded": len(report.errors),
    }
