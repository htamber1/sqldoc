"""Compliance expansion: per-regulation reports, data lineage, and access audit.

Builds on the PII scanner (:mod:`sqldoc.pii`) and the extracted schema — no row
data is read:

* **Per-regulation reports** — group the scan findings by HIPAA / GDPR / PCI-DSS
  and pair each regulation with the controls it typically requires, so you can
  see exactly which tables/columns pull the database into each regime.
* **Data lineage** — parse view/procedure definitions to trace how data flows
  between tables (a view reads its source tables; a proc's ``INSERT … SELECT``
  is a directional table-to-table flow).
* **Access audit** — read object-level grants (``sys.database_permissions`` on
  SQL Server; ``information_schema.table_privileges`` on PostgreSQL and MySQL)
  and cross-reference them with the PII findings: "which principals can read
  regulated columns".

Per-regulation reporting and data lineage are dialect-neutral (they run on the
extracted dataclasses); the access audit dispatches on the adapter's dialect.
"""
import re
from dataclasses import dataclass, field

from sqldoc.dbutil import cell
from sqldoc.pii import RISK_ORDER

REGULATIONS = ["HIPAA", "GDPR", "PCI-DSS"]

# Representative control requirements per regulation. Not legal advice — a
# starting checklist mapped to the data the scanner flagged.
REGULATION_CONTROLS = {
    "HIPAA": [
        "Encrypt ePHI at rest and in transit (Security Rule §164.312(a)(2)(iv), (e)).",
        "Enforce role-based access control and unique user IDs (§164.312(a)(2)(i)).",
        "Log and audit access to PHI; retain audit trails (§164.312(b)).",
        "Mask/de-identify PHI in non-production environments (§164.514).",
        "Maintain Business Associate Agreements with any downstream processors.",
    ],
    "GDPR": [
        "Establish a lawful basis and record processing purposes (Art. 6, 30).",
        "Apply data minimization and storage limitation (Art. 5(1)(c),(e)).",
        "Encrypt/pseudonymize personal data (Art. 32).",
        "Support data-subject rights: access, erasure, portability (Art. 15-20).",
        "Apply extra safeguards + explicit consent for special-category data (Art. 9).",
    ],
    "PCI-DSS": [
        "Never store sensitive authentication data (CVV/PIN) after authorization (Req. 3.2).",
        "Render the PAN unreadable — tokenize or strong-encrypt (Req. 3.4).",
        "Restrict cardholder-data access to least privilege (Req. 7).",
        "Assign unique IDs and log all access to cardholder data (Req. 8, 10).",
        "Scope reduction: segment systems that store/process cardholder data (Req. 1).",
    ],
}


@dataclass
class RegulationSection:
    regulation: str
    findings: list = field(default_factory=list)      # Finding objects
    controls: list = field(default_factory=list)

    @property
    def table_count(self) -> int:
        return len({(f.schema, f.table) for f in self.findings})

    @property
    def column_count(self) -> int:
        return len(self.findings)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.risk == "HIGH")


@dataclass
class DataFlow:
    source: str          # schema.table
    target: str          # schema.view / schema.proc / schema.table
    via: str             # the mediating view/procedure (schema.name)
    kind: str            # view / procedure-read / procedure-write


@dataclass
class Permission:
    principal: str
    principal_type: str
    permission: str
    state: str           # GRANT / DENY / GRANT_WITH_GRANT_OPTION
    schema: str
    object: str
    object_type: str


@dataclass
class RoleMember:
    role: str
    member: str
    member_type: str = ""


@dataclass
class PrincipalAccess:
    """One unified row per principal (user OR role) aggregating every grant it
    holds across all objects in the database."""
    principal: str
    principal_type: str = ""
    is_role: bool = False
    levels: list = field(default_factory=list)        # subset of read/write/admin
    permissions: list = field(default_factory=list)   # distinct permission names
    object_count: int = 0                             # objects it can touch
    pii_object_count: int = 0                         # of those, ones holding PII
    max_risk: str = "NONE"
    regulations: list = field(default_factory=list)
    members: list = field(default_factory=list)       # expanded role members (names)


@dataclass
class AccessAlert:
    principal: str
    permission: str
    schema: str
    table: str
    max_risk: str
    categories: list = field(default_factory=list)
    regulations: list = field(default_factory=list)


@dataclass
class ComplianceReport:
    database: str
    regulations: list = field(default_factory=list)
    lineage: list = field(default_factory=list)
    permissions: list = field(default_factory=list)
    access_alerts: list = field(default_factory=list)
    role_members: list = field(default_factory=list)   # RoleMember rows
    principals: list = field(default_factory=list)     # PrincipalAccess rows
    errors: list = field(default_factory=list)


# --- per-regulation --------------------------------------------------------

def build_regulation_sections(findings) -> list:
    sections = []
    for reg in REGULATIONS:
        matched = [f for f in findings if reg in f.regulations]
        # Sort strongest-risk first for the report.
        matched.sort(key=lambda f: (-RISK_ORDER.get(f.risk, 0), f.schema, f.table, f.column))
        sections.append(RegulationSection(
            regulation=reg, findings=matched,
            controls=list(REGULATION_CONTROLS.get(reg, [])),
        ))
    return sections


# --- data lineage ----------------------------------------------------------

# INSERT INTO [schema.]table, tolerating any dialect's identifier quoting
# (SQL Server [ ], PostgreSQL/ANSI " ", MySQL ` `).
_Q = r'["\[\]`]?'
_INSERT_INTO = re.compile(rf"insert\s+into\s+(?:{_Q}(\w+){_Q}\.)?{_Q}(\w+){_Q}", re.IGNORECASE)


def _mentions(definition, table_name) -> bool:
    if not definition:
        return False
    return re.search(rf"\b{re.escape(table_name)}\b", definition, re.IGNORECASE) is not None


def build_lineage(tables, views=None, procedures=None) -> list:
    views = views or []
    procedures = procedures or []
    by_name = {t.name.lower(): t for t in tables}
    flows = []

    for v in views:
        for t in tables:
            if _mentions(v.definition, t.name):
                flows.append(DataFlow(source=f"{t.schema}.{t.name}",
                                      target=f"{v.schema}.{v.name}", via=f"{v.schema}.{v.name}",
                                      kind="view"))

    for p in procedures:
        referenced = [t for t in tables if _mentions(p.definition, t.name)]
        # Directional INSERT ... target(s): a table written to by this proc.
        targets = set()
        for m in _INSERT_INTO.finditer(p.definition or ""):
            tgt = by_name.get((m.group(2) or "").lower())
            if tgt:
                targets.add(tgt.name)
        for t in referenced:
            if t.name in targets:
                # Everything else this proc reads flows into the target table.
                for src in referenced:
                    if src.name != t.name:
                        flows.append(DataFlow(source=f"{src.schema}.{src.name}",
                                              target=f"{t.schema}.{t.name}", via=f"{p.schema}.{p.name}",
                                              kind="procedure-write"))
            else:
                flows.append(DataFlow(source=f"{t.schema}.{t.name}",
                                      target=f"{p.schema}.{p.name}", via=f"{p.schema}.{p.name}",
                                      kind="procedure-read"))
    return flows


# --- access audit ----------------------------------------------------------

_SQLSERVER_GRANTS = """
    SELECT
        pr.name AS principal_name,
        pr.type_desc AS principal_type,
        perm.permission_name AS permission_name,
        perm.state_desc AS state_desc,
        s.name AS schema_name,
        o.name AS object_name,
        o.type_desc AS object_type
    FROM sys.database_permissions perm
    INNER JOIN sys.database_principals pr ON perm.grantee_principal_id = pr.principal_id
    INNER JOIN sys.objects o ON perm.major_id = o.object_id
    INNER JOIN sys.schemas s ON o.schema_id = s.schema_id
    WHERE perm.class = 1 AND perm.minor_id = 0 AND o.type IN ('U', 'V')
    ORDER BY s.name, o.name, pr.name, perm.permission_name
"""

# information_schema.table_privileges is standard SQL; PostgreSQL and MySQL both
# expose it. It only lists GRANTs (no DENY), so state is always "GRANT".
_PG_GRANTS = """
    SELECT grantee, table_schema, table_name, privilege_type
    FROM information_schema.table_privileges
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY table_schema, table_name, grantee, privilege_type
"""

_MYSQL_GRANTS = """
    SELECT grantee, table_schema, table_name, privilege_type
    FROM information_schema.table_privileges
    WHERE table_schema = DATABASE()
    ORDER BY table_schema, table_name, grantee, privilege_type
"""


def extract_permissions(adapter) -> list:
    """Object-level grants for the adapter's dialect, as Permission rows.

    SQL Server reads ``sys.database_permissions`` (rich: includes DENY);
    PostgreSQL/MySQL read the standard ``information_schema.table_privileges``
    (GRANTs only). Other dialects have no ported access audit and return [].
    """
    dialect = getattr(adapter, "dialect", "sqlserver")
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        if dialect in ("sqlserver", "azuresql"):
            cursor.execute(_SQLSERVER_GRANTS)
            return [
                Permission(
                    principal=cell(r, "principal_name"), principal_type=cell(r, "principal_type"),
                    permission=cell(r, "permission_name"), state=cell(r, "state_desc"),
                    schema=cell(r, "schema_name"), object=cell(r, "object_name"),
                    object_type=cell(r, "object_type"),
                )
                for r in cursor.fetchall()
            ]
        if dialect in ("postgres", "mysql"):
            cursor.execute(_PG_GRANTS if dialect == "postgres" else _MYSQL_GRANTS)
            return [
                Permission(
                    principal=cell(r, "grantee"), principal_type="",
                    permission=cell(r, "privilege_type"), state="GRANT",
                    schema=cell(r, "table_schema"), object=cell(r, "table_name"),
                    object_type="",
                )
                for r in cursor.fetchall()
            ]
        return []
    finally:
        conn.close()


def build_access_alerts(permissions, findings) -> list:
    """Grants that land on a table carrying PII findings — read access to
    regulated data. DENY grants are ignored (they remove, not grant, access)."""
    by_table = {}
    for f in findings:
        key = (f.schema, f.table)
        info = by_table.setdefault(key, {"risk": "LOW", "categories": set(), "regs": set()})
        if RISK_ORDER.get(f.risk, 0) > RISK_ORDER.get(info["risk"], 0):
            info["risk"] = f.risk
        info["categories"].add(f.category)
        info["regs"].update(f.regulations)

    alerts = []
    for p in permissions:
        if p.state.startswith("DENY"):
            continue
        info = by_table.get((p.schema, p.object))
        if not info:
            continue
        alerts.append(AccessAlert(
            principal=p.principal, permission=p.permission,
            schema=p.schema, table=p.object, max_risk=info["risk"],
            categories=sorted(info["categories"]), regulations=sorted(info["regs"]),
        ))
    alerts.sort(key=lambda a: (-RISK_ORDER.get(a.max_risk, 0), a.schema, a.table, a.principal))
    return alerts


# --- permission level + role membership ------------------------------------

LEVELS = ["read", "write", "admin"]
_LEVEL_ORDER = {"read": 0, "write": 1, "admin": 2}

# Object-level permission names, bucketed into a coarse read/write/admin level.
_WRITE_PERMS = {"INSERT", "UPDATE", "DELETE", "EXECUTE", "TRUNCATE", "TRIGGER"}
# Admin/DDL-ish rights that let a principal reshape or fully own an object.
_ADMIN_PERMS = {"CONTROL", "ALTER", "TAKE OWNERSHIP", "OWNERSHIP", "ALL"}


def classify_permission(permission, state="") -> str:
    """Bucket an object-level permission into read / write / admin.

    GRANT WITH GRANT OPTION is treated as admin (the principal can re-grant).
    Anything not clearly write or admin (SELECT, REFERENCES, VIEW DEFINITION, …)
    counts as read.
    """
    p = (permission or "").strip().upper()
    if state and "WITH_GRANT" in state.upper():
        return "admin"
    if p in _ADMIN_PERMS:
        return "admin"
    if p in _WRITE_PERMS:
        return "write"
    return "read"


# Role/group membership. SQL Server exposes sys.database_role_members;
# PostgreSQL exposes pg_auth_members. MySQL role edges are version-specific and
# not read here (its access audit is table-grant only).
_SQLSERVER_ROLE_MEMBERS = """
    SELECT r.name AS role_name, m.name AS member_name, m.type_desc AS member_type
    FROM sys.database_role_members rm
    INNER JOIN sys.database_principals r ON rm.role_principal_id = r.principal_id
    INNER JOIN sys.database_principals m ON rm.member_principal_id = m.principal_id
    ORDER BY r.name, m.name
"""

_PG_ROLE_MEMBERS = """
    SELECT r.rolname AS role_name, m.rolname AS member_name, 'ROLE' AS member_type
    FROM pg_auth_members am
    INNER JOIN pg_roles r ON am.roleid = r.oid
    INNER JOIN pg_roles m ON am.member = m.oid
    ORDER BY r.rolname, m.rolname
"""


def extract_role_members(adapter) -> list:
    """Database role/group memberships for the adapter's dialect.

    SQL Server and PostgreSQL are supported; other dialects return []. Read-only
    catalog access — no row data.
    """
    dialect = getattr(adapter, "dialect", "sqlserver")
    if dialect not in ("sqlserver", "azuresql", "postgres"):
        return []
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        sql = _SQLSERVER_ROLE_MEMBERS if dialect in ("sqlserver", "azuresql") else _PG_ROLE_MEMBERS
        cursor.execute(sql)
        return [
            RoleMember(role=cell(r, "role_name"), member=cell(r, "member_name"),
                       member_type=cell(r, "member_type"))
            for r in cursor.fetchall()
        ]
    finally:
        conn.close()


def build_principal_summary(permissions, findings, role_members=None) -> list:
    """Collapse the object-level grants into one unified row per principal.

    Each principal gets its read/write/admin levels, distinct permission names,
    the count of objects it can touch (and how many hold PII, with the worst
    risk + regulations across them), and — if it is a database role — the
    expanded list of its members. DENY grants are excluded.
    """
    role_members = role_members or []
    members_by_role = {}
    for rm in role_members:
        members_by_role.setdefault(rm.role, []).append(rm.member)
    role_names = set(members_by_role)

    # PII risk per table, mirroring build_access_alerts.
    by_table = {}
    for f in findings:
        info = by_table.setdefault((f.schema, f.table),
                                   {"risk": "LOW", "regs": set()})
        if RISK_ORDER.get(f.risk, 0) > RISK_ORDER.get(info["risk"], 0):
            info["risk"] = f.risk
        info["regs"].update(f.regulations)

    aggs = {}

    def _agg(name, ptype=""):
        a = aggs.get(name)
        if a is None:
            a = {"principal": name, "type": ptype, "levels": set(),
                 "perms": set(), "objects": set(), "pii_objects": set(),
                 "risk": "NONE", "regs": set()}
            aggs[name] = a
        elif ptype and not a["type"]:
            a["type"] = ptype
        return a

    for p in permissions:
        if p.state.startswith("DENY"):
            continue
        a = _agg(p.principal, p.principal_type)
        a["levels"].add(classify_permission(p.permission, p.state))
        a["perms"].add(p.permission)
        a["objects"].add((p.schema, p.object))
        info = by_table.get((p.schema, p.object))
        if info:
            a["pii_objects"].add((p.schema, p.object))
            if RISK_ORDER.get(info["risk"], 0) > RISK_ORDER.get(a["risk"], 0):
                a["risk"] = info["risk"]
            a["regs"].update(info["regs"])

    # Roles with members but no direct grants still deserve a row (so their
    # membership is visible in the expansion).
    for role in role_names:
        _agg(role, "ROLE")

    summary = []
    for name, a in aggs.items():
        summary.append(PrincipalAccess(
            principal=name,
            principal_type=a["type"],
            is_role=name in role_names,
            levels=sorted(a["levels"], key=lambda l: _LEVEL_ORDER[l]),
            permissions=sorted(a["perms"]),
            object_count=len(a["objects"]),
            pii_object_count=len(a["pii_objects"]),
            max_risk=a["risk"],
            regulations=sorted(a["regs"]),
            members=sorted(members_by_role.get(name, [])),
        ))
    # Riskiest, widest-reaching principals first.
    summary.sort(key=lambda pa: (-RISK_ORDER.get(pa.max_risk, 0),
                                 -pa.pii_object_count, -pa.object_count, pa.principal))
    return summary


# --- orchestration ---------------------------------------------------------

def collect_compliance(database, tables, findings, views=None, procedures=None,
                       adapter=None) -> ComplianceReport:
    report = ComplianceReport(database=database)
    report.regulations = build_regulation_sections(findings)
    report.lineage = build_lineage(tables, views, procedures)
    if adapter is not None:
        try:
            report.permissions = extract_permissions(adapter)
        except Exception as e:
            report.errors.append(("Access audit (object grants)",
                                  f"{type(e).__name__}: {e}"))
        try:
            report.role_members = extract_role_members(adapter)
        except Exception as e:
            report.errors.append(("Role membership expansion",
                                  f"{type(e).__name__}: {e}"))
    report.access_alerts = build_access_alerts(report.permissions, findings)
    report.principals = build_principal_summary(
        report.permissions, findings, report.role_members)
    return report


def summarize(report: ComplianceReport) -> dict:
    return {
        "regulations_in_scope": sum(1 for s in report.regulations if s.findings),
        "hipaa": next((s.column_count for s in report.regulations if s.regulation == "HIPAA"), 0),
        "gdpr": next((s.column_count for s in report.regulations if s.regulation == "GDPR"), 0),
        "pci_dss": next((s.column_count for s in report.regulations if s.regulation == "PCI-DSS"), 0),
        "lineage_flows": len(report.lineage),
        "access_alerts": len(report.access_alerts),
        "high_risk_grants": sum(1 for a in report.access_alerts if a.max_risk == "HIGH"),
        "principals": len(report.principals),
        "roles": sum(1 for p in report.principals if p.is_role),
        "principals_with_pii": sum(1 for p in report.principals if p.pii_object_count),
        "degraded": len(report.errors),
    }
