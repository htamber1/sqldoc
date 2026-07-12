"""Multi-database unified access report — the board-level compliance view.

Given several configured databases, this collapses their per-database access
audits into ONE cross-database matrix: every principal (user/role) as a row, one
column per database, each cell showing the access that principal holds there
(read/write/admin level, how many PII-bearing objects, worst risk). It answers
"who can touch regulated data, and where, across our whole estate" in a single
report.

Pure aggregation over the existing :mod:`sqldoc.comply` primitives — the CLI
does the per-database extraction and hands the results here.
"""
from dataclasses import dataclass, field

from sqldoc.pii import RISK_ORDER
from sqldoc.comply import build_principal_summary


@dataclass
class DatabaseAccess:
    """The per-database access audit result feeding the cross-DB matrix."""
    database: str
    principals: list = field(default_factory=list)     # PrincipalAccess rows
    error: str = ""


@dataclass
class CrossDbPrincipal:
    principal: str
    is_role: bool = False
    per_db: dict = field(default_factory=dict)          # db name -> PrincipalAccess
    database_count: int = 0                             # DBs where it has access
    total_pii_objects: int = 0
    max_risk: str = "NONE"
    levels: list = field(default_factory=list)          # union across DBs


@dataclass
class MultiComplyReport:
    databases: list = field(default_factory=list)       # ordered DB names
    principals: list = field(default_factory=list)      # CrossDbPrincipal rows
    errors: list = field(default_factory=list)          # (database, message)


def collect_database_access(name, findings, permissions, role_members=None) -> DatabaseAccess:
    """Build one database's principal summary from its scan findings + grants."""
    principals = build_principal_summary(permissions, findings, role_members or [])
    return DatabaseAccess(database=name, principals=principals)


_LEVEL_ORDER = {"read": 0, "write": 1, "admin": 2}


def build_cross_db(db_access_list) -> MultiComplyReport:
    """Merge per-database :class:`DatabaseAccess` results into a cross-database
    matrix keyed by principal name."""
    report = MultiComplyReport()
    for da in db_access_list:
        report.databases.append(da.database)
        if da.error:
            report.errors.append((da.database, da.error))

    by_principal = {}
    for da in db_access_list:
        for pa in da.principals:
            cp = by_principal.get(pa.principal)
            if cp is None:
                cp = CrossDbPrincipal(principal=pa.principal, is_role=pa.is_role)
                by_principal[pa.principal] = cp
            cp.is_role = cp.is_role or pa.is_role
            cp.per_db[da.database] = pa
            if pa.object_count:
                cp.database_count += 1
            cp.total_pii_objects += pa.pii_object_count
            if RISK_ORDER.get(pa.max_risk, 0) > RISK_ORDER.get(cp.max_risk, 0):
                cp.max_risk = pa.max_risk
            for lv in pa.levels:
                if lv not in cp.levels:
                    cp.levels.append(lv)

    for cp in by_principal.values():
        cp.levels.sort(key=lambda l: _LEVEL_ORDER.get(l, 9))

    # Board view: broadest reach + highest risk first.
    report.principals = sorted(
        by_principal.values(),
        key=lambda c: (-c.database_count, -RISK_ORDER.get(c.max_risk, 0),
                       -c.total_pii_objects, c.principal))
    return report


def summarize_multi(report: MultiComplyReport) -> dict:
    principals = report.principals
    return {
        "databases": len(report.databases),
        "principals": len(principals),
        "roles": sum(1 for p in principals if p.is_role),
        "cross_db_principals": sum(1 for p in principals if p.database_count > 1),
        "principals_with_pii": sum(1 for p in principals if p.total_pii_objects),
        "high_risk_principals": sum(1 for p in principals if p.max_risk == "HIGH"),
        "errors": len(report.errors),
    }
