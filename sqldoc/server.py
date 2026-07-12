"""Instance-level SQL Server health + SQL Agent job monitoring.

Where :mod:`sqldoc.health` looks at one database, this looks at the whole SQL
Server **instance** via server-scoped DMVs and the ``msdb`` catalog:

* **CPU** — ``sys.dm_os_ring_buffers`` (RING_BUFFER_SCHEDULER_MONITOR) split into
  SQL / other-process / idle, plus core/scheduler counts from
  ``sys.dm_os_sys_info``.
* **Memory** — ``sys.dm_os_memory_clerks`` broken into buffer pool / plan cache /
  stolen / other.
* **Disk** — ``sys.dm_os_volume_stats`` (free space per volume) joined with
  ``sys.dm_io_virtual_file_stats`` (read/write latency per drive).
* **Uptime** — ``sqlserver_start_time`` from ``sys.dm_os_sys_info``.
* **Connections + blocking** — ``sys.dm_exec_sessions`` / ``sys.dm_exec_requests``
  (active requests, blocking chains, top consumers running right now).
* **SQL Agent jobs** — ``msdb.dbo.sysjobs`` / ``sysjobhistory`` / ``sysjobsteps``
  / ``sysjobschedules``: last run status + duration, step-level failure
  messages, jobs failed in the last 24h, long runners over their average, and
  next scheduled run.

Every check runs in its own try/except: server-scoped DMVs need ``VIEW SERVER
STATE`` and the Agent views need msdb access, so a permission failure degrades
that one section to a note instead of failing the whole report. Reads only
server statistics + job history — never table row data. SQL Server only.
"""
from dataclasses import dataclass, field

from sqldoc.dbutil import cell


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


def _collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# --- dataclasses -----------------------------------------------------------

@dataclass
class ServerInfo:
    cpu_count: int = 0
    scheduler_count: int = 0
    hyperthread_ratio: int = 0
    physical_memory_mb: int = 0
    sql_server_start_time: str = ""
    uptime_seconds: int = 0

    @property
    def uptime_text(self) -> str:
        return _fmt_duration(self.uptime_seconds)


@dataclass
class CpuUsage:
    sql_process_percent: int = 0
    other_process_percent: int = 0
    idle_percent: int = 0
    sample_time: str = ""


@dataclass
class MemoryBreakdown:
    total_mb: float = 0.0
    buffer_pool_mb: float = 0.0
    plan_cache_mb: float = 0.0
    stolen_mb: float = 0.0
    other_mb: float = 0.0
    clerks: list = field(default_factory=list)   # (clerk_type, mb)


@dataclass
class VolumeHealth:
    volume: str
    logical_name: str = ""
    total_gb: float = 0.0
    available_gb: float = 0.0
    read_latency_ms: float = 0.0
    write_latency_ms: float = 0.0

    @property
    def used_gb(self) -> float:
        return round(self.total_gb - self.available_gb, 1)

    @property
    def used_percent(self) -> float:
        return round(100.0 * (self.total_gb - self.available_gb) / self.total_gb, 1) if self.total_gb else 0.0

    @property
    def free_percent(self) -> float:
        return round(100.0 * self.available_gb / self.total_gb, 1) if self.total_gb else 0.0

    @property
    def is_low(self) -> bool:
        return self.total_gb > 0 and self.free_percent < 10.0


@dataclass
class ActiveRequest:
    session_id: int
    login_name: str = ""
    host_name: str = ""
    database: str = ""
    status: str = ""
    command: str = ""
    blocking_session_id: int = 0
    wait_type: str = ""
    cpu_ms: int = 0
    elapsed_ms: int = 0
    reads: int = 0
    query_text: str = ""


@dataclass
class BlockingChain:
    blocker_session_id: int
    blocked_session_id: int
    wait_type: str = ""
    wait_time_ms: int = 0
    blocked_query: str = ""
    blocker_query: str = ""


@dataclass
class ConnectionSummary:
    total_sessions: int = 0
    active_requests: int = 0
    blocked_requests: int = 0
    by_login: list = field(default_factory=list)      # (login, count)
    by_database: list = field(default_factory=list)   # (database, count)


# --- SQL Agent job dataclasses ---------------------------------------------

@dataclass
class JobStepFailure:
    step_id: int
    step_name: str
    message: str


@dataclass
class AgentJob:
    name: str
    enabled: bool = True
    last_run_status: str = ""          # Succeeded / Failed / Retry / Canceled / In progress / Unknown
    last_run_time: str = ""
    last_run_duration_seconds: int = 0
    avg_duration_seconds: int = 0
    next_run_time: str = ""
    category: str = ""
    owner: str = ""
    failed_last_24h: bool = False
    step_failures: list = field(default_factory=list)  # JobStepFailure

    @property
    def is_long_running(self) -> bool:
        # Flag the last run when it ran materially longer than its average.
        return (self.avg_duration_seconds > 0
                and self.last_run_duration_seconds > 1.5 * self.avg_duration_seconds
                and self.last_run_duration_seconds - self.avg_duration_seconds >= 30)

    @property
    def duration_text(self) -> str:
        return _fmt_duration(self.last_run_duration_seconds)


@dataclass
class TempDbSession:
    session_id: int
    login: str = ""
    user_mb: float = 0.0
    internal_mb: float = 0.0

    @property
    def total_mb(self) -> float:
        return round(self.user_mb + self.internal_mb, 1)


@dataclass
class TempDbReport:
    version_store_mb: float = 0.0
    version_generation_kb_s: int = 0
    version_cleanup_kb_s: int = 0
    file_count: int = 0
    data_file_count: int = 0
    total_size_mb: float = 0.0
    recommended_files: int = 0
    pagelatch_contention: int = 0
    autogrowth_events: int = 0
    top_sessions: list = field(default_factory=list)   # TempDbSession
    notes: list = field(default_factory=list)


@dataclass
class ServerReport:
    server_name: str
    dialect: str = "sqlserver"
    info: ServerInfo = None
    cpu: CpuUsage = None
    memory: MemoryBreakdown = None
    volumes: list = field(default_factory=list)
    connections: ConnectionSummary = None
    blocking_chains: list = field(default_factory=list)
    top_queries: list = field(default_factory=list)      # ActiveRequest
    agent_jobs: list = field(default_factory=list)        # AgentJob
    tempdb: object = None                                 # TempDbReport
    backups: object = None                                # BackupReport
    errors: list = field(default_factory=list)            # (section, message)


# --- collectors ------------------------------------------------------------

def collect_server_info(cursor) -> ServerInfo:
    cursor.execute("""
        SELECT cpu_count,
               scheduler_count,
               hyperthread_ratio,
               physical_memory_kb / 1024 AS physical_memory_mb,
               sqlserver_start_time,
               DATEDIFF(SECOND, sqlserver_start_time, GETDATE()) AS uptime_seconds
        FROM sys.dm_os_sys_info
    """)
    r = cursor.fetchall()
    if not r:
        return ServerInfo()
    r = r[0]
    return ServerInfo(
        cpu_count=_i(cell(r, "cpu_count")),
        scheduler_count=_i(cell(r, "scheduler_count")),
        hyperthread_ratio=_i(cell(r, "hyperthread_ratio")),
        physical_memory_mb=_i(cell(r, "physical_memory_mb")),
        sql_server_start_time=_s(cell(r, "sqlserver_start_time")),
        uptime_seconds=_i(cell(r, "uptime_seconds")),
    )


def collect_cpu(cursor) -> CpuUsage:
    # Latest scheduler-monitor snapshot from the ring buffer (shredded XML).
    cursor.execute("""
        SELECT TOP 1
            record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS sql_cpu,
            record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS idle_cpu,
            100
              - record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int')
              - record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS other_cpu,
            record.value('(./Record/@id)[1]', 'int') AS record_id
        FROM (
            SELECT CONVERT(xml, record) AS record
            FROM sys.dm_os_ring_buffers
            WHERE ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR'
              AND record LIKE '%<SystemHealth>%'
        ) AS x
        ORDER BY record_id DESC
    """)
    rows = cursor.fetchall()
    if not rows:
        return CpuUsage()
    r = rows[0]
    sql = _i(cell(r, "sql_cpu"))
    idle = _i(cell(r, "idle_cpu"))
    other = _i(cell(r, "other_cpu"))
    # On Linux SQL Server the scheduler-monitor ring buffer often reports 0 for
    # both SQL and idle utilization, which would make "other" a misleading 100%.
    # Treat an all-zero sample as "no data" rather than 100% other-process.
    if sql == 0 and idle == 0:
        other = 0
    else:
        other = max(0, other)
    return CpuUsage(
        sql_process_percent=sql,
        other_process_percent=other,
        idle_percent=idle,
        sample_time=_s(cell(r, "record_id")),
    )


# Clerk types that make up the plan cache.
_PLAN_CACHE_TYPES = {"CACHESTORE_SQLCP", "CACHESTORE_OBJCP", "CACHESTORE_PHDR",
                     "CACHESTORE_XPROC", "CACHESTORE_TEMPTABLES"}
_BUFFER_POOL_TYPE = "MEMORYCLERK_SQLBUFFERPOOL"


def collect_memory(cursor) -> MemoryBreakdown:
    cursor.execute("""
        SELECT [type] AS clerk_type,
               CAST(SUM(pages_kb) / 1024.0 AS DECIMAL(18,1)) AS mb
        FROM sys.dm_os_memory_clerks
        GROUP BY [type]
        HAVING SUM(pages_kb) > 0
        ORDER BY SUM(pages_kb) DESC
    """)
    mem = MemoryBreakdown()
    for r in cursor.fetchall():
        ctype = _s(cell(r, "clerk_type"))
        mb = _f(cell(r, "mb"))
        mem.total_mb += mb
        if ctype == _BUFFER_POOL_TYPE:
            mem.buffer_pool_mb += mb
        elif ctype in _PLAN_CACHE_TYPES:
            mem.plan_cache_mb += mb
        mem.clerks.append((ctype, mb))
    mem.total_mb = round(mem.total_mb, 1)
    mem.buffer_pool_mb = round(mem.buffer_pool_mb, 1)
    mem.plan_cache_mb = round(mem.plan_cache_mb, 1)
    # Stolen ~= everything that is not buffer pool (SQL Server's "stolen" pages).
    mem.stolen_mb = round(mem.total_mb - mem.buffer_pool_mb, 1)
    mem.other_mb = round(mem.total_mb - mem.buffer_pool_mb - mem.plan_cache_mb, 1)
    mem.clerks = mem.clerks[:12]
    return mem


def collect_volumes(cursor) -> list:
    # Key volumes by the first char of the data-file path (drive letter on
    # Windows, "/" on Linux) so the I/O-latency query can be merged onto them.
    # On Linux volume_mount_point is NULL, so fall back to a readable label.
    cursor.execute("""
        SELECT
            vs.volume_mount_point,
            vs.logical_volume_name,
            CAST(vs.total_bytes / 1073741824.0 AS DECIMAL(18,1)) AS total_gb,
            CAST(vs.available_bytes / 1073741824.0 AS DECIMAL(18,1)) AS available_gb,
            LEFT(mf.physical_name, 1) AS drive
        FROM sys.master_files AS mf
        CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) AS vs
    """)
    by_drive = {}
    for r in cursor.fetchall():
        drive = _s(cell(r, "drive")).upper()
        if drive in by_drive:
            continue
        mount = _s(cell(r, "volume_mount_point"))
        if mount:
            label = mount
        elif drive == "/":
            label = "/ (root)"
        elif drive:
            label = f"{drive}:\\"
        else:
            label = "(default)"
        by_drive[drive] = VolumeHealth(
            volume=label,
            logical_name=_s(cell(r, "logical_volume_name")),
            total_gb=_f(cell(r, "total_gb")),
            available_gb=_f(cell(r, "available_gb")),
        )

    # I/O latency per drive, merged onto the matching volume by drive key.
    try:
        cursor.execute("""
            SELECT LEFT(mf.physical_name, 1) AS drive,
                   CASE WHEN SUM(vfs.num_of_reads) = 0 THEN 0
                        ELSE SUM(vfs.io_stall_read_ms) / SUM(vfs.num_of_reads) END AS read_latency_ms,
                   CASE WHEN SUM(vfs.num_of_writes) = 0 THEN 0
                        ELSE SUM(vfs.io_stall_write_ms) / SUM(vfs.num_of_writes) END AS write_latency_ms
            FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
            JOIN sys.master_files AS mf
              ON vfs.database_id = mf.database_id AND vfs.file_id = mf.file_id
            GROUP BY LEFT(mf.physical_name, 1)
        """)
        for r in cursor.fetchall():
            drive = _s(cell(r, "drive")).upper()
            vh = by_drive.get(drive)
            if vh:
                vh.read_latency_ms = _f(cell(r, "read_latency_ms"))
                vh.write_latency_ms = _f(cell(r, "write_latency_ms"))
    except Exception:
        pass  # latency is best-effort; keep the space figures

    return sorted(by_drive.values(), key=lambda v: v.free_percent)


def collect_active_requests(cursor, top: int) -> list:
    cursor.execute(f"""
        SELECT TOP ({int(top)})
            r.session_id,
            s.login_name,
            s.host_name,
            DB_NAME(r.database_id) AS database_name,
            r.status,
            r.command,
            r.blocking_session_id,
            r.wait_type,
            r.cpu_time AS cpu_ms,
            r.total_elapsed_time AS elapsed_ms,
            r.reads,
            SUBSTRING(t.text, (r.statement_start_offset/2)+1,
                ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(t.text)
                  ELSE r.statement_end_offset END - r.statement_start_offset)/2)+1) AS query_text
        FROM sys.dm_exec_requests AS r
        INNER JOIN sys.dm_exec_sessions AS s ON r.session_id = s.session_id
        OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) AS t
        WHERE s.is_user_process = 1 AND r.session_id <> @@SPID
        ORDER BY r.cpu_time DESC
    """)
    out = []
    for r in cursor.fetchall():
        out.append(ActiveRequest(
            session_id=_i(cell(r, "session_id")),
            login_name=_s(cell(r, "login_name")),
            host_name=_s(cell(r, "host_name")),
            database=_s(cell(r, "database_name")),
            status=_s(cell(r, "status")),
            command=_s(cell(r, "command")),
            blocking_session_id=_i(cell(r, "blocking_session_id")),
            wait_type=_s(cell(r, "wait_type")),
            cpu_ms=_i(cell(r, "cpu_ms")),
            elapsed_ms=_i(cell(r, "elapsed_ms")),
            reads=_i(cell(r, "reads")),
            query_text=_collapse_ws(_s(cell(r, "query_text")))[:400],
        ))
    return out


def collect_connections(cursor) -> ConnectionSummary:
    cursor.execute("""
        SELECT login_name, DB_NAME(database_id) AS database_name, status
        FROM sys.dm_exec_sessions
        WHERE is_user_process = 1
    """)
    by_login, by_db = {}, {}
    total = 0
    for r in cursor.fetchall():
        total += 1
        login = _s(cell(r, "login_name")) or "(unknown)"
        db = _s(cell(r, "database_name")) or "(none)"
        by_login[login] = by_login.get(login, 0) + 1
        by_db[db] = by_db.get(db, 0) + 1
    summary = ConnectionSummary(total_sessions=total)
    summary.by_login = sorted(by_login.items(), key=lambda kv: -kv[1])[:12]
    summary.by_database = sorted(by_db.items(), key=lambda kv: -kv[1])[:12]
    return summary


def build_blocking_chains(requests) -> list:
    """From the active requests, pair each blocked request with its blocker."""
    by_session = {r.session_id: r for r in requests}
    chains = []
    for r in requests:
        if r.blocking_session_id and r.blocking_session_id != 0:
            blocker = by_session.get(r.blocking_session_id)
            chains.append(BlockingChain(
                blocker_session_id=r.blocking_session_id,
                blocked_session_id=r.session_id,
                wait_type=r.wait_type,
                wait_time_ms=r.elapsed_ms,
                blocked_query=r.query_text,
                blocker_query=(blocker.query_text if blocker else ""),
            ))
    return chains


# --- SQL Agent job collectors ----------------------------------------------

_JOB_STATUS = {0: "Failed", 1: "Succeeded", 2: "Retry", 3: "Canceled", 4: "In progress"}


def collect_agent_jobs(cursor) -> list:
    """Jobs with their last outcome, average duration, step-level failures, and
    next scheduled run. Reads msdb only."""
    cursor.execute("""
        SELECT j.job_id,
               j.name AS job_name,
               j.enabled,
               SUSER_SNAME(j.owner_sid) AS owner,
               c.name AS category,
               ja.run_status AS last_run_status,
               ja.run_datetime AS last_run_time,
               ja.run_duration_seconds,
               agg.avg_duration_seconds,
               sched.next_run_datetime
        FROM msdb.dbo.sysjobs AS j
        LEFT JOIN msdb.dbo.syscategories AS c ON j.category_id = c.category_id
        OUTER APPLY (
            SELECT TOP 1 h.run_status,
                   msdb.dbo.agent_datetime(h.run_date, h.run_time) AS run_datetime,
                   (h.run_duration/10000*3600) + ((h.run_duration/100)%100*60) + (h.run_duration%100) AS run_duration_seconds
            FROM msdb.dbo.sysjobhistory AS h
            WHERE h.job_id = j.job_id AND h.step_id = 0
            ORDER BY h.run_date DESC, h.run_time DESC
        ) AS ja
        OUTER APPLY (
            SELECT AVG((h.run_duration/10000*3600) + ((h.run_duration/100)%100*60) + (h.run_duration%100)) AS avg_duration_seconds
            FROM msdb.dbo.sysjobhistory AS h
            WHERE h.job_id = j.job_id AND h.step_id = 0
        ) AS agg
        OUTER APPLY (
            SELECT MIN(js.next_run_date) AS next_run_datetime
            FROM msdb.dbo.sysjobschedules AS js
            WHERE js.job_id = j.job_id
        ) AS sched
        ORDER BY j.name
    """)
    jobs = []
    job_rows = cursor.fetchall()
    for r in job_rows:
        status_code = cell(r, "last_run_status")
        status = _JOB_STATUS.get(_i(status_code), "Unknown") if status_code is not None else "Never run"
        # sysjobschedules.next_run_date is 0 until SQL Agent computes it (and is
        # 0 while the Agent service is stopped) — show that as "no next run".
        next_run = _s(cell(r, "next_run_datetime"))
        if next_run in ("0", "", "None"):
            next_run = ""
        jobs.append(AgentJob(
            name=_s(cell(r, "job_name")),
            enabled=bool(_i(cell(r, "enabled"))),
            last_run_status=status,
            last_run_time=_s(cell(r, "last_run_time")),
            last_run_duration_seconds=_i(cell(r, "run_duration_seconds")),
            avg_duration_seconds=_i(cell(r, "avg_duration_seconds")),
            next_run_time=next_run,
            category=_s(cell(r, "category")),
            owner=_s(cell(r, "owner")),
        ))

    # Step-level failure messages from the last 24 hours.
    try:
        cursor.execute("""
            SELECT j.name AS job_name, h.step_id, h.step_name, h.message
            FROM msdb.dbo.sysjobhistory AS h
            INNER JOIN msdb.dbo.sysjobs AS j ON h.job_id = j.job_id
            WHERE h.run_status = 0 AND h.step_id > 0
              AND msdb.dbo.agent_datetime(h.run_date, h.run_time) >= DATEADD(HOUR, -24, GETDATE())
            ORDER BY h.run_date DESC, h.run_time DESC
        """)
        fails_by_job = {}
        for r in cursor.fetchall():
            fails_by_job.setdefault(_s(cell(r, "job_name")), []).append(JobStepFailure(
                step_id=_i(cell(r, "step_id")),
                step_name=_s(cell(r, "step_name")),
                message=_collapse_ws(_s(cell(r, "message")))[:500],
            ))
        for job in jobs:
            if job.name in fails_by_job:
                job.step_failures = fails_by_job[job.name]
                job.failed_last_24h = True
    except Exception:
        pass

    # Also mark a job failed-in-24h if its last outcome was Failed.
    for job in jobs:
        if job.last_run_status == "Failed" and job.step_failures:
            job.failed_last_24h = True
    return jobs


# --- TempDB monitoring (SQL Server) ----------------------------------------

def collect_tempdb(cursor, cpu_count: int = 0) -> TempDbReport:
    """TempDB health: version store size + generation/cleanup rates, file layout,
    top session consumers, and current system-page (PFS/GAM/SGAM) latch
    contention. Each sub-query is isolated so a missing permission degrades that
    piece only. `cpu_count` (from the instance info) sets the recommended file
    count."""
    report = TempDbReport()
    recommended = min(8, cpu_count or 8)

    def sub(fn):
        try:
            fn()
        except Exception as e:
            report.notes.append(f"partial: {type(e).__name__}: {e}")

    def vstore():
        cursor.execute("""
            SELECT
                (SELECT ISNULL(SUM(version_store_reserved_page_count), 0) * 8 / 1024.0
                 FROM tempdb.sys.dm_db_file_space_usage) AS version_store_mb,
                (SELECT ISNULL(MAX(cntr_value), 0) FROM sys.dm_os_performance_counters
                 WHERE counter_name LIKE 'Version Generation rate%') AS version_gen_kb,
                (SELECT ISNULL(MAX(cntr_value), 0) FROM sys.dm_os_performance_counters
                 WHERE counter_name LIKE 'Version Cleanup rate%') AS version_cleanup_kb
        """)
        rows = cursor.fetchall()
        if rows:
            r = rows[0]
            report.version_store_mb = _f(cell(r, "version_store_mb"))
            report.version_generation_kb_s = _i(cell(r, "version_gen_kb"))
            report.version_cleanup_kb_s = _i(cell(r, "version_cleanup_kb"))

    def files():
        cursor.execute("""
            SELECT COUNT(*) AS file_count,
                   SUM(CASE WHEN type_desc = 'ROWS' THEN 1 ELSE 0 END) AS data_files,
                   SUM(CAST(size AS bigint)) * 8 / 1024.0 AS total_size_mb
            FROM sys.master_files WHERE database_id = DB_ID('tempdb')
        """)
        rows = cursor.fetchall()
        if rows:
            r = rows[0]
            report.file_count = _i(cell(r, "file_count"))
            report.data_file_count = _i(cell(r, "data_files"))
            report.total_size_mb = _f(cell(r, "total_size_mb"))
            report.recommended_files = recommended
            if report.data_file_count < report.recommended_files:
                report.notes.append(
                    f"TempDB has {report.data_file_count} data file(s); "
                    f"{report.recommended_files} are recommended (= min(8, cores)) to reduce "
                    f"allocation-page contention.")

    def sessions():
        cursor.execute("""
            SELECT TOP 10 su.session_id,
                   su.user_objects_alloc_page_count * 8 / 1024.0 AS user_mb,
                   su.internal_objects_alloc_page_count * 8 / 1024.0 AS internal_mb,
                   s.login_name
            FROM sys.dm_db_session_space_usage su
            LEFT JOIN sys.dm_exec_sessions s ON su.session_id = s.session_id
            WHERE su.user_objects_alloc_page_count + su.internal_objects_alloc_page_count > 0
            ORDER BY (su.user_objects_alloc_page_count + su.internal_objects_alloc_page_count) DESC
        """)
        for r in cursor.fetchall():
            report.top_sessions.append(TempDbSession(
                session_id=_i(cell(r, "session_id")), login=_s(cell(r, "login_name")),
                user_mb=_f(cell(r, "user_mb")), internal_mb=_f(cell(r, "internal_mb"))))

    def contention():
        cursor.execute("""
            SELECT COUNT(*) AS contention
            FROM sys.dm_os_waiting_tasks
            WHERE wait_type LIKE 'PAGELATCH%' AND resource_description LIKE '2:%'
        """)
        rows = cursor.fetchall()
        if rows:
            report.pagelatch_contention = _i(cell(rows[0], "contention"))

    def autogrowth():
        cursor.execute("""
            DECLARE @p nvarchar(260) = (SELECT path FROM sys.traces WHERE is_default = 1);
            SELECT COUNT(*) AS growth_events
            FROM sys.fn_trace_gettable(@p, DEFAULT)
            WHERE EventClass IN (92, 93) AND DatabaseName = 'tempdb';
        """)
        rows = cursor.fetchall()
        if rows:
            report.autogrowth_events = _i(cell(rows[0], "growth_events"))

    sub(vstore)
    sub(files)
    sub(sessions)
    sub(contention)
    sub(autogrowth)
    return report


# --- orchestration ---------------------------------------------------------

def collect_server(adapter, top: int = 10, include_jobs: bool = True,
                   include_backups: bool = True) -> ServerReport:
    """Run the instance-level checks appropriate to the adapter. Each check is
    isolated so a missing permission (VIEW SERVER STATE / msdb access) degrades
    to a note in `report.errors` rather than failing the whole run.

    The CPU/memory/disk/session/job sections are SQL-Server-specific; on other
    dialects only the cross-dialect sections (backups) are collected."""
    from sqldoc.backup import collect_backups_from_cursor, BACKUP_DIALECTS
    dialect = getattr(adapter, "dialect", "sqlserver")
    report = ServerReport(server_name="", dialect=dialect)
    conn = adapter.connect()
    cursor = adapter.cursor(conn)

    def run(label, fn):
        try:
            return fn()
        except Exception as e:
            report.errors.append((label, f"{type(e).__name__}: {e}"))
            return None

    try:
        if dialect in ("sqlserver", "azuresql"):
            report.info = run("Server info", lambda: collect_server_info(cursor))
            report.cpu = run("CPU usage", lambda: collect_cpu(cursor))
            report.memory = run("Memory", lambda: collect_memory(cursor))
            report.volumes = run("Disk volumes", lambda: collect_volumes(cursor)) or []
            report.connections = run("Connections", lambda: collect_connections(cursor))
            requests = run("Active requests", lambda: collect_active_requests(cursor, top)) or []
            report.top_queries = requests
            report.blocking_chains = build_blocking_chains(requests)
            if report.connections is not None:
                report.connections.active_requests = len(requests)
                report.connections.blocked_requests = len(report.blocking_chains)
            if include_jobs:
                report.agent_jobs = run("SQL Agent jobs", lambda: collect_agent_jobs(cursor)) or []
            cpu_count = report.info.cpu_count if report.info else 0
            report.tempdb = run("TempDB", lambda: collect_tempdb(cursor, cpu_count))
        if include_backups and dialect in BACKUP_DIALECTS:
            report.backups = run("Backups", lambda: collect_backups_from_cursor(dialect, cursor))
    finally:
        conn.close()
    return report


def summarize(report: ServerReport) -> dict:
    jobs = report.agent_jobs
    return {
        "cpu_sql_percent": report.cpu.sql_process_percent if report.cpu else 0,
        "memory_total_mb": report.memory.total_mb if report.memory else 0,
        "uptime": report.info.uptime_text if report.info else "",
        "volumes": len(report.volumes),
        "low_disk_volumes": sum(1 for v in report.volumes if v.is_low),
        "sessions": report.connections.total_sessions if report.connections else 0,
        "blocking_chains": len(report.blocking_chains),
        "active_requests": len(report.top_queries),
        "jobs": len(jobs),
        "failed_jobs_24h": sum(1 for j in jobs if j.failed_last_24h),
        "disabled_jobs": sum(1 for j in jobs if not j.enabled),
        "long_running_jobs": sum(1 for j in jobs if j.is_long_running),
        "backup_databases": len(report.backups.databases) if report.backups else 0,
        "never_backed_up": (sum(1 for d in report.backups.databases if d.never_backed_up)
                            if report.backups else 0),
        "backup_issues": (sum(1 for d in report.backups.databases if d.issues)
                          if report.backups else 0),
        "pitr_enabled": report.backups.pitr_enabled if report.backups else False,
        "tempdb_version_store_mb": report.tempdb.version_store_mb if report.tempdb else 0.0,
        "tempdb_contention": report.tempdb.pagelatch_contention if report.tempdb else 0,
        "tempdb_data_files": report.tempdb.data_file_count if report.tempdb else 0,
        "degraded": len(report.errors),
    }
