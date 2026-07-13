"""Run a command across every CMS-registered server in parallel and aggregate.

Each command has a *worker* that opens an adapter for one server and returns a
compact summary dict (the same shape for every server, since they all run the
same command). ``run_bulk`` fans the workers out over a thread pool; a server
that fails is captured as a :class:`ServerResult` with the error rather than
stopping the whole run.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from sqldoc.cms import connection_string_for, select_servers


@dataclass
class ServerResult:
    server: str
    host: str
    group: str = ""
    ok: bool = True
    error: str = ""
    summary: dict = field(default_factory=dict)


def _adapter_for(server, opts, database=None):
    from sqldoc.adapters import get_adapter
    cs = connection_string_for(
        server.server_name, database=database or opts.get("database") or "master",
        windows_auth=opts.get("windows_auth", True),
        username=opts.get("username"), password=opts.get("password"))
    return get_adapter(cs, "sqlserver")


def _tables(adapter, opts):
    tables = adapter.extract_metadata()
    if opts.get("schemas"):
        allow = [s.strip() for s in opts["schemas"].split(",")]
        tables = [t for t in tables if t.schema in allow]
    return tables


# --- per-command workers ---------------------------------------------------

def _w_doc(server, opts):
    a = _adapter_for(server, opts)
    tables = _tables(a, opts)
    return {"database": opts.get("database") or "master", "tables": len(tables),
            "views": len(a.extract_views()), "procedures": len(a.extract_procedures()),
            "columns": sum(len(t.columns) for t in tables)}


def _w_scan(server, opts):
    from sqldoc.pii import scan_tables, summarize
    a = _adapter_for(server, opts)
    s = summarize(scan_tables(_tables(a, opts)))
    return {"database": opts.get("database") or "master", "total": s["total"],
            "HIGH": s["by_risk"]["HIGH"], "MEDIUM": s["by_risk"]["MEDIUM"],
            "LOW": s["by_risk"]["LOW"], "tables": s["tables_affected"]}


def _w_health(server, opts):
    from sqldoc.health import collect_health, summarize
    return dict(summarize(collect_health(_adapter_for(server, opts))))


def _w_quality(server, opts):
    from sqldoc.quality import collect_quality, summarize
    a = _adapter_for(server, opts)
    return dict(summarize(collect_quality(a, _tables(a, opts))))


def _w_intel(server, opts):
    from sqldoc.intel import collect_intel, summarize
    a = _adapter_for(server, opts)
    t = _tables(a, opts)
    return dict(summarize(collect_intel(server.name, t, a.extract_views(), a.extract_procedures())))


def _w_comply(server, opts):
    from sqldoc.pii import scan_tables
    from sqldoc.comply import collect_compliance, summarize
    a = _adapter_for(server, opts)
    t = _tables(a, opts)
    rep = collect_compliance(server.name, t, scan_tables(t), a.extract_views(),
                             a.extract_procedures(), adapter=a)
    return dict(summarize(rep))


def _w_server(server, opts):
    from sqldoc.server import collect_server, summarize
    return dict(summarize(collect_server(_adapter_for(server, opts))))


def _w_secure(server, opts):
    from sqldoc.secure import collect_security, summarize
    return dict(summarize(collect_security(_adapter_for(server, opts))))


def _w_backup(server, opts):
    from sqldoc.backup import collect_backups, summarize
    return dict(summarize(collect_backups(_adapter_for(server, opts))))


WORKERS = {
    "doc": _w_doc, "scan": _w_scan, "health": _w_health, "quality": _w_quality,
    "intel": _w_intel, "comply": _w_comply, "server": _w_server, "secure": _w_secure,
    "backup": _w_backup,
}

CMS_COMMANDS = tuple(WORKERS)


# --- runner ----------------------------------------------------------------

def _run_one(worker, server, opts):
    r = ServerResult(server=server.name, host=server.server_name, group=server.group_path)
    try:
        r.summary = worker(server, opts)
    except Exception as e:
        r.ok = False
        r.error = f"{type(e).__name__}: {e}"
    return r


def run_against_servers(servers, worker, opts, max_workers=8):
    if not servers:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(servers)))) as ex:
        futs = [ex.submit(_run_one, worker, s, opts) for s in servers]
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: (r.group, r.server))
    return results


def run_bulk(inventory, command, opts, group=None, max_workers=8):
    if command not in WORKERS:
        raise ValueError(f"'{command}' does not support --cms bulk runs "
                         f"(supported: {', '.join(CMS_COMMANDS)}).")
    servers = select_servers(inventory, group)
    return run_against_servers(servers, WORKERS[command], opts, max_workers)
