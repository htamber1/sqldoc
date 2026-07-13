"""Backup / point-in-time-recovery monitoring across dialects.

Answers "are my databases actually being backed up, and can I recover to a
point in time?" — dialect by dialect, because each engine records backups
differently:

* **SQL Server** — ``msdb.dbo.backupset`` joined with ``sys.databases``: last
  full / differential / log backup per database, the recovery model, and
  mismatches (a FULL-recovery database with no log backups will grow its log
  unbounded and can't do point-in-time restore properly).
* **PostgreSQL** — ``pg_stat_archiver`` + the ``archive_mode`` setting: WAL
  archiving is the PITR mechanism, so ``archive_mode = on`` with a recent
  ``last_archived_time`` (and no archive failures) is the health signal.
* **MySQL** — binary logging (``log_bin``) is the PITR proxy: without it there
  is no point-in-time recovery. Databases are enumerated from
  ``information_schema.schemata``.

Reads only backup/catalog metadata — never table row data.
"""
from dataclasses import dataclass, field

from sqldoc.dbutil import cell

BACKUP_DIALECTS = {"sqlserver", "azuresql", "azure_managed_instance", "postgres", "mysql"}


def _s(v) -> str:
    return "" if v is None else str(v)


def _f(v):
    try:
        return None if v is None else round(float(v), 1)
    except (TypeError, ValueError):
        return None


@dataclass
class DatabaseBackup:
    database: str
    recovery_model: str = ""
    last_full_backup: str = ""
    last_diff_backup: str = ""
    last_log_backup: str = ""
    last_backup_age_hours: float = None
    never_backed_up: bool = False
    pitr_capable: bool = False
    issues: list = field(default_factory=list)


@dataclass
class BackupReport:
    dialect: str = ""
    supported: bool = True
    pitr_enabled: bool = False           # instance-level PITR mechanism on?
    pitr_mechanism: str = ""             # "log backups" / "WAL archiving" / "binary logging"
    databases: list = field(default_factory=list)   # DatabaseBackup
    archiver: dict = None                # PG pg_stat_archiver snapshot
    notes: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# --- SQL Server ------------------------------------------------------------

def _collect_sqlserver(cursor) -> BackupReport:
    report = BackupReport(dialect="sqlserver", pitr_mechanism="log backups")
    cursor.execute("""
        SELECT d.name AS database_name,
               d.recovery_model_desc,
               MAX(CASE WHEN b.type = 'D' THEN b.backup_finish_date END) AS last_full,
               MAX(CASE WHEN b.type = 'I' THEN b.backup_finish_date END) AS last_diff,
               MAX(CASE WHEN b.type = 'L' THEN b.backup_finish_date END) AS last_log,
               DATEDIFF(HOUR, MAX(CASE WHEN b.type = 'D' THEN b.backup_finish_date END), GETDATE()) AS full_age_hours
        FROM sys.databases d
        LEFT JOIN msdb.dbo.backupset b ON b.database_name = d.name
        WHERE d.name NOT IN ('tempdb')
        GROUP BY d.name, d.recovery_model_desc
        ORDER BY d.name
    """)
    any_pitr = False
    for r in cursor.fetchall():
        rec = _s(cell(r, "recovery_model_desc")).upper()
        last_full = _s(cell(r, "last_full"))
        last_log = _s(cell(r, "last_log"))
        db = DatabaseBackup(
            database=_s(cell(r, "database_name")),
            recovery_model=rec,
            last_full_backup=last_full,
            last_diff_backup=_s(cell(r, "last_diff")),
            last_log_backup=last_log,
            last_backup_age_hours=_f(cell(r, "full_age_hours")),
            never_backed_up=not last_full,
            pitr_capable=rec in ("FULL", "BULK_LOGGED"),
        )
        if db.never_backed_up:
            db.issues.append("Never backed up.")
        if rec in ("FULL", "BULK_LOGGED") and not last_log:
            db.issues.append(f"{rec} recovery model but no log backups — the transaction "
                             f"log will grow unbounded and point-in-time restore is incomplete.")
        if rec == "SIMPLE" and db.database not in ("master", "model", "msdb"):
            db.issues.append("SIMPLE recovery model — no point-in-time recovery.")
        any_pitr = any_pitr or db.pitr_capable
        report.databases.append(db)
    report.pitr_enabled = any_pitr
    return report


# --- PostgreSQL ------------------------------------------------------------

def _collect_postgres(cursor) -> BackupReport:
    report = BackupReport(dialect="postgres", pitr_mechanism="WAL archiving")
    cursor.execute("SELECT setting FROM pg_settings WHERE name = 'archive_mode'")
    rows = cursor.fetchall()
    archive_mode = (_s(cell(rows[0], "setting")).lower() if rows else "off")
    report.pitr_enabled = archive_mode in ("on", "always")

    cursor.execute("""
        SELECT last_archived_time, last_archived_wal, archived_count,
               failed_count, last_failed_time,
               EXTRACT(EPOCH FROM (now() - last_archived_time)) / 3600.0 AS age_hours
        FROM pg_stat_archiver
    """)
    arch = cursor.fetchall()
    age = None
    if arch:
        a = arch[0]
        report.archiver = {
            "last_archived_time": _s(cell(a, "last_archived_time")),
            "last_archived_wal": _s(cell(a, "last_archived_wal")),
            "archived_count": int(cell(a, "archived_count") or 0),
            "failed_count": int(cell(a, "failed_count") or 0),
            "last_failed_time": _s(cell(a, "last_failed_time")),
        }
        age = _f(cell(a, "age_hours"))
        if report.archiver["failed_count"]:
            report.notes.append(f"{report.archiver['failed_count']} WAL archive failure(s) "
                                f"recorded (last: {report.archiver['last_failed_time'] or 'n/a'}).")

    if not report.pitr_enabled:
        report.notes.append("archive_mode is off — no WAL archiving, so no point-in-time recovery.")

    cursor.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
    for r in cursor.fetchall():
        name = _s(cell(r, "datname"))
        db = DatabaseBackup(
            database=name,
            recovery_model=("WAL archiving on" if report.pitr_enabled else "WAL archiving off"),
            last_full_backup=(report.archiver or {}).get("last_archived_time", ""),
            last_backup_age_hours=age,
            never_backed_up=not report.pitr_enabled and not (report.archiver or {}).get("last_archived_time"),
            pitr_capable=report.pitr_enabled,
        )
        if not report.pitr_enabled:
            db.issues.append("No WAL archiving — point-in-time recovery not possible.")
        report.databases.append(db)
    return report


# --- MySQL -----------------------------------------------------------------

def _collect_mysql(cursor) -> BackupReport:
    report = BackupReport(dialect="mysql", pitr_mechanism="binary logging")
    cursor.execute("SELECT @@log_bin AS log_bin, @@log_bin_basename AS basename")
    rows = cursor.fetchall()
    log_bin = False
    if rows:
        log_bin = str(cell(rows[0], "log_bin")) in ("1", "ON", "True", "true")
    report.pitr_enabled = log_bin
    if not log_bin:
        report.notes.append("Binary logging (log_bin) is OFF — no point-in-time recovery. "
                            "MySQL does not track backups natively; binlog is the PITR proxy.")
    else:
        report.notes.append("Binary logging is ON (point-in-time recovery capable). MySQL does "
                            "not record backup history in the catalog — verify your backup job separately.")

    cursor.execute("""
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')
        ORDER BY schema_name
    """)
    for r in cursor.fetchall():
        name = _s(cell(r, "schema_name"))
        db = DatabaseBackup(
            database=name,
            recovery_model=("binary logging on" if log_bin else "binary logging off"),
            pitr_capable=log_bin,
            never_backed_up=False,        # MySQL can't tell from the catalog
        )
        if not log_bin:
            db.issues.append("No binary logging — point-in-time recovery not possible.")
        report.databases.append(db)
    return report


# --- dispatch --------------------------------------------------------------

def _collect_azure_mi(cursor) -> BackupReport:
    """Azure SQL Managed Instance backups are managed by Azure — surface the
    automated-backup status from sys.dm_database_backups rather than msdb."""
    report = BackupReport(dialect="azure_managed_instance", pitr_mechanism="Azure automated backups")
    report.pitr_enabled = True     # Azure always keeps automated PITR backups
    report.notes.append("Backups are managed automatically by Azure (point-in-time restore is always on). "
                        "Showing the latest automated backup per database.")
    cursor.execute("""
        SELECT database_name,
               MAX(CASE WHEN backup_type = 'FULL' THEN backup_finish_date END) AS last_full,
               MAX(CASE WHEN backup_type = 'DIFF' THEN backup_finish_date END) AS last_diff,
               MAX(CASE WHEN backup_type = 'LOG' THEN backup_finish_date END) AS last_log,
               DATEDIFF(HOUR, MAX(CASE WHEN backup_type = 'FULL' THEN backup_finish_date END), GETDATE()) AS full_age_hours
        FROM sys.dm_database_backups
        GROUP BY database_name
        ORDER BY database_name
    """)
    for r in cursor.fetchall():
        last_full = _s(cell(r, "last_full"))
        db = DatabaseBackup(
            database=_s(cell(r, "database_name")),
            recovery_model="Azure managed",
            last_full_backup=last_full,
            last_diff_backup=_s(cell(r, "last_diff")),
            last_log_backup=_s(cell(r, "last_log")),
            last_backup_age_hours=_f(cell(r, "full_age_hours")),
            never_backed_up=not last_full,
            pitr_capable=True,
        )
        if db.never_backed_up:
            db.issues.append("No automated backup recorded yet (new database?).")
        report.databases.append(db)
    return report


def collect_backups_from_cursor(dialect, cursor) -> BackupReport:
    if dialect == "azure_managed_instance":
        return _collect_azure_mi(cursor)
    if dialect in ("sqlserver", "azuresql", "azure_managed_instance"):
        return _collect_sqlserver(cursor)
    if dialect == "postgres":
        return _collect_postgres(cursor)
    if dialect == "mysql":
        return _collect_mysql(cursor)
    return BackupReport(dialect=dialect, supported=False,
                        notes=[f"Backup monitoring is not implemented for {dialect}."])


def collect_backups(adapter) -> BackupReport:
    """Backup / PITR status for the adapter's dialect. Opens its own connection."""
    dialect = getattr(adapter, "dialect", "sqlserver")
    if dialect not in BACKUP_DIALECTS:
        return BackupReport(dialect=dialect, supported=False,
                            notes=[f"Backup monitoring is not implemented for {dialect}."])
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        return collect_backups_from_cursor(dialect, cursor)
    finally:
        conn.close()


def summarize(report: BackupReport) -> dict:
    dbs = report.databases
    return {
        "databases": len(dbs),
        "never_backed_up": sum(1 for d in dbs if d.never_backed_up),
        "with_issues": sum(1 for d in dbs if d.issues),
        "pitr_enabled": report.pitr_enabled,
        "pitr_mechanism": report.pitr_mechanism,
    }


def build_backup_json(database: str, report: BackupReport) -> dict:
    from dataclasses import asdict
    return {
        "report_type": "backup", "database": database, "summary": summarize(report),
        "pitr_enabled": report.pitr_enabled, "pitr_mechanism": report.pitr_mechanism,
        "databases": [asdict(d) for d in report.databases],
        "notes": report.notes, "errors": report.errors,
    }


def render_backup_html(database: str, report: BackupReport, output_path: str):
    import html as _h
    s = summarize(report)
    rows = "".join(
        f"<tr><td>{_h.escape(d.database)}</td><td>{_h.escape(d.recovery_model or '-')}</td>"
        f"<td>{_h.escape(str(d.last_full_backup or 'never'))}</td>"
        f"<td>{_h.escape(str(d.last_log_backup or '-'))}</td>"
        f"<td>{'yes' if d.never_backed_up else 'no'}</td>"
        f"<td>{_h.escape('; '.join(d.issues) or '-')}</td></tr>"
        for d in report.databases)
    css = ("body{background:#0d1117;color:#c9d1d9;font:14px -apple-system,Segoe UI,sans-serif;"
           "margin:0;padding:24px}table{border-collapse:collapse;width:100%}"
           "th,td{border:1px solid #21262d;padding:6px 9px;text-align:left}"
           "th{background:#161b22;color:#8b949e}h1{font-size:19px}")
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><title>Backups - {_h.escape(database)}</title>"
           f"<style>{css}</style></head><body><h1>Backup status: {_h.escape(database)}</h1>"
           f"<p>PITR: {'enabled' if report.pitr_enabled else 'disabled'} "
           f"({_h.escape(report.pitr_mechanism or 'n/a')}) &middot; {s['databases']} database(s), "
           f"{s['never_backed_up']} never backed up, {s['with_issues']} with issues.</p>"
           f"<table><tr><th>Database</th><th>Recovery</th><th>Last full</th><th>Last log</th>"
           f"<th>Never</th><th>Issues</th></tr>{rows}</table></body></html>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(doc)


def stale_databases(report: BackupReport, max_age_hours: float) -> list:
    """Databases whose most recent backup is older than `max_age_hours`, or that
    have never been backed up (used by the agent alert)."""
    out = []
    for d in report.databases:
        if d.never_backed_up:
            out.append(d)
        elif d.last_backup_age_hours is not None and d.last_backup_age_hours > max_age_hours:
            out.append(d)
    return out
