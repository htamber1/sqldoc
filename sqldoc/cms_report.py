"""CMS inventory report — document the registered-server estate for audits and
capacity planning: SQL Server version, edition, uptime, database count, and the
last successful sqldoc run per server."""
from sqldoc.dbutil import cell

PROBE_SQL = """
    /* CMS_PROBE */
    SELECT
        CAST(SERVERPROPERTY('ProductVersion') AS varchar(50))  AS version,
        CAST(SERVERPROPERTY('Edition') AS varchar(100))        AS edition,
        CAST(SERVERPROPERTY('ProductLevel') AS varchar(20))    AS product_level,
        (SELECT COUNT(*) FROM sys.databases WHERE database_id > 4) AS db_count,
        DATEDIFF(HOUR, (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), GETDATE()) AS uptime_hours
"""


def probe_server(cursor) -> dict:
    cursor.execute(PROBE_SQL)
    r = cursor.fetchall()[0]
    return {
        "version": cell(r, "version"), "edition": cell(r, "edition"),
        "product_level": cell(r, "product_level"),
        "db_count": int(cell(r, "db_count") or 0),
        "uptime_hours": int(cell(r, "uptime_hours") or 0),
    }


def _worker(server, opts):
    from sqldoc.cms_bulk import _adapter_for
    adapter = _adapter_for(server, opts, database="master")
    conn = adapter.connect()
    try:
        return probe_server(adapter.cursor(conn))
    finally:
        conn.close()


def _last_run(store, name):
    if store is None:
        return None
    try:
        run = store.last_run(name)
    except Exception:
        return None
    if not run:
        return None
    return run.get("finished_at") or run.get("started_at")


def collect_report(inventory, opts, store=None, group=None, max_workers=8) -> list:
    from sqldoc.cms_bulk import run_against_servers
    from sqldoc.cms import select_servers
    servers = select_servers(inventory, group)
    results = run_against_servers(servers, _worker, opts, max_workers)
    for r in results:
        if r.ok:
            r.summary["last_run"] = _last_run(store, r.server)
    return results


def build_report_json(inventory, results) -> dict:
    return {
        "report_type": "cms-inventory-report",
        "cms": inventory.cms_server,
        "server_count": len(results),
        "reachable": sum(1 for r in results if r.ok),
        "servers": [{
            "server": r.server, "host": r.host, "group": r.group, "reachable": r.ok,
            "error": r.error,
            **({k: r.summary.get(k) for k in ("version", "edition", "product_level",
                                              "db_count", "uptime_hours", "last_run")}
               if r.ok else {}),
        } for r in results],
    }
