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
    report.access_alerts = build_access_alerts(report.permissions, findings)
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
        "degraded": len(report.errors),
    }
