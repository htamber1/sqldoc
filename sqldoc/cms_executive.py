"""CMS executive dashboard — a board-level rollup across the whole SQL Server
estate. Each registered server is scored exactly as ``sqldoc executive`` scores a
single database; the per-server summaries are then aggregated into one estate
scorecard with the top risks across every server."""
from dataclasses import dataclass, field

from sqldoc.cms_bulk import _adapter_for, run_against_servers
from sqldoc.cms import select_servers

_SEV_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


@dataclass
class EstateSummary:
    results: list = field(default_factory=list)     # ServerResult per server
    server_count: int = 0
    database_count: int = 0
    pii_total: int = 0
    overall: object = None
    health: object = None
    pii_safety: object = None
    backup: object = None
    security: object = None
    top_risks: list = field(default_factory=list)

    @property
    def failed(self):
        return [r for r in self.results if not r.ok]


def _db_count(adapter) -> int:
    try:
        conn = adapter.connect()
        try:
            cur = adapter.cursor(conn)
            cur.execute("SELECT COUNT(*) FROM sys.databases WHERE database_id > 4")
            return int(cur.fetchall()[0][0])
        finally:
            conn.close()
    except Exception:
        return 1


def _exec_worker(server, opts):
    from sqldoc.pii import scan_tables
    from sqldoc.health import collect_health, summarize as health_summarize
    from sqldoc.backup import collect_backups
    from sqldoc.secure import collect_security
    from sqldoc import executive as executive_mod

    a = _adapter_for(server, opts)
    tables = a.extract_metadata()
    findings = scan_tables(tables)
    caps = a.capabilities
    hs = bs = ss = None
    if getattr(caps, "health", False):
        try:
            hs = health_summarize(collect_health(a))
        except Exception:
            hs = None
    if getattr(caps, "infra_monitoring", False):
        try:
            bs = collect_backups(a)
        except Exception:
            bs = None
        try:
            ss = collect_security(a)
        except Exception:
            ss = None
    summ = executive_mod.build_summary(server.name, health_summary=hs, findings=findings,
                                       backup_report=bs, security_report=ss, previous=None)
    return {
        "server": server.name, "host": server.server_name, "group": server.group_path,
        "overall": summ.overall_score, "overall_label": summ.overall_label,
        "pii_safety": summ.pii_safety_score, "backup_pct": summ.backup_compliance_pct,
        "security": summ.security_score, "security_grade": summ.security_grade,
        "health": summ.health_score, "pii_findings": len(findings), "tables": len(tables),
        "db_count": _db_count(a), "top_risks": summ.top_risks,
    }


def _avg(ok, key):
    vals = [r.summary.get(key) for r in ok if isinstance(r.summary.get(key), (int, float))]
    return round(sum(vals) / len(vals)) if vals else None


def aggregate(results) -> EstateSummary:
    ok = [r for r in results if r.ok]
    top = []
    for r in ok:
        for risk in (r.summary.get("top_risks") or []):
            top.append({**risk, "server": r.server})
    top.sort(key=lambda x: _SEV_ORDER.get(x.get("severity"), 4))
    return EstateSummary(
        results=results,
        server_count=len(ok),
        database_count=sum(r.summary.get("db_count", 0) for r in ok),
        pii_total=sum(r.summary.get("pii_findings", 0) for r in ok),
        overall=_avg(ok, "overall"), health=_avg(ok, "health"),
        pii_safety=_avg(ok, "pii_safety"), backup=_avg(ok, "backup_pct"),
        security=_avg(ok, "security"), top_risks=top[:10])


def collect_estate(inventory, opts, group=None, max_workers=8) -> EstateSummary:
    servers = select_servers(inventory, group)
    results = run_against_servers(servers, _exec_worker, opts, max_workers)
    return aggregate(results)


def build_estate_json(estate: EstateSummary) -> dict:
    return {
        "report_type": "cms-executive",
        "server_count": estate.server_count, "database_count": estate.database_count,
        "pii_total": estate.pii_total,
        "scores": {"overall": estate.overall, "health": estate.health,
                   "data_protection": estate.pii_safety, "backup_compliance_pct": estate.backup,
                   "security": estate.security},
        "top_risks": estate.top_risks,
        "servers": [{"server": r.server, "host": r.host, "group": r.group, "ok": r.ok,
                     "error": r.error, "scores": {k: r.summary.get(k) for k in
                                                  ("overall", "health", "pii_safety", "backup_pct",
                                                   "security", "security_grade", "pii_findings")}}
                    for r in estate.results],
    }
