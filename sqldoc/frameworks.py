"""Compliance-framework assessment mapped to specific control numbers.

Extends `sqldoc comply` beyond HIPAA/GDPR/PCI-DSS to the major enterprise
frameworks — SOX, FedRAMP (NIST 800-53), ISO 27001, CMMC, CCPA, PIPEDA, and
SOC 2. Each framework's controls are evaluated against signals already collected
by comply (PII findings, object grants reaching PII, principal privilege levels,
segregation-of-duties), producing per-control assessments with the specific
findings that drove them.

This is a mapping aid, not a certification: it surfaces where the metadata shows
a control needs attention. Auditing/logging controls are always flagged for
review since sqldoc can't observe the audit configuration from schema metadata.
"""
from dataclasses import dataclass, field

# framework id -> (display name, [ (control_id, title, check) ])
FRAMEWORKS = {
    "sox": ("SOX (Sarbanes-Oxley)", [
        ("ITGC-AC", "Access to financial data is restricted (IT general controls)", "least_privilege"),
        ("Section-404", "Segregation of duties over financial data", "sod"),
        ("Section-302", "Audit trail of access changes", "audit"),
        ("COBIT-DSS05", "Sensitive/financial data inventory", "pii_present"),
    ]),
    "fedramp": ("FedRAMP (NIST 800-53)", [
        ("AC-2", "Account Management", "least_privilege"),
        ("AC-3", "Access Enforcement", "broad_pii_access"),
        ("AC-6", "Least Privilege", "least_privilege"),
        ("AC-5", "Separation of Duties", "sod"),
        ("AU-2", "Audit Events", "audit"),
    ]),
    "iso27001": ("ISO/IEC 27001:2022", [
        ("A.5.15", "Access control", "broad_pii_access"),
        ("A.5.18", "Access rights (provisioning/least privilege)", "least_privilege"),
        ("A.5.3", "Segregation of duties", "sod"),
        ("A.5.12", "Classification of information (sensitive data)", "pii_present"),
        ("A.8.15", "Logging", "audit"),
    ]),
    "cmmc": ("CMMC 2.0", [
        ("AC.L2-3.1.1", "Limit system access to authorized users", "least_privilege"),
        ("AC.L2-3.1.5", "Employ least privilege", "least_privilege"),
        ("AC.L2-3.1.4", "Separate duties of individuals", "sod"),
        ("AC.L2-3.1.3", "Control the flow of CUI", "broad_pii_access"),
        ("AU.L2-3.3.1", "Create and retain audit logs", "audit"),
    ]),
    "ccpa": ("CCPA (California Consumer Privacy Act)", [
        ("1798.100", "Right to know — personal information inventory", "pii_present"),
        ("1798.150", "Reasonable security for personal information", "broad_pii_access"),
        ("1798.105", "Right to deletion — sensitive data mapping", "pii_high"),
    ]),
    "pipeda": ("PIPEDA (Canada)", [
        ("Principle-4.1", "Accountability — personal data inventory", "pii_present"),
        ("Principle-4.7", "Safeguards", "broad_pii_access"),
        ("Principle-4.5", "Limiting use, disclosure & retention", "pii_high"),
    ]),
    "soc2": ("SOC 2 Type II (Trust Services Criteria)", [
        ("CC6.1", "Logical access security controls", "broad_pii_access"),
        ("CC6.3", "Role-based access / least privilege", "least_privilege"),
        ("CC6.2", "User registration & authorization (SoD)", "sod"),
        ("CC7.2", "Monitoring of controls", "audit"),
    ]),
}

FRAMEWORK_CHOICES = list(FRAMEWORKS.keys())


@dataclass
class ControlAssessment:
    control_id: str
    title: str
    status: str            # attention | review | pass
    detail: str
    findings: list = field(default_factory=list)


@dataclass
class FrameworkResult:
    framework: str         # id
    name: str
    controls: list = field(default_factory=list)

    @property
    def summary(self) -> dict:
        out = {"attention": 0, "review": 0, "pass": 0}
        for c in self.controls:
            out[c.status] = out.get(c.status, 0) + 1
        return out


def _signals(ctx) -> dict:
    findings = ctx.get("pii_findings") or []
    principals = ctx.get("principals") or []
    alerts = ctx.get("access_alerts") or []
    return {
        "pii_tables": sorted({f"{f.schema}.{f.table}" for f in findings}),
        "pii_high": [f for f in findings if getattr(f, "risk", "") == "HIGH"],
        "alerts": alerts,
        "admin": [p for p in principals if "admin" in (getattr(p, "levels", []) or [])],
        "sod": [p for p in principals
                if "write" in (getattr(p, "levels", []) or [])
                and "admin" in (getattr(p, "levels", []) or [])],
    }


def _run_check(name, s):
    """Return (status, detail, findings) for a check name."""
    if name == "pii_present":
        t = s["pii_tables"]
        return ("attention" if t else "pass",
                f"{len(t)} table(s) hold regulated/sensitive data." if t
                else "No sensitive data detected.", t[:50])
    if name == "pii_high":
        h = s["pii_high"]
        return ("attention" if h else "pass",
                f"{len(h)} HIGH-risk column(s) present." if h else "No HIGH-risk columns.",
                [f"{f.schema}.{f.table}.{f.column}" for f in h[:50]])
    if name == "broad_pii_access":
        a = s["alerts"]
        return ("attention" if a else "pass",
                f"{len(a)} grant(s) reach PII-bearing tables." if a
                else "No object grants reach PII tables.",
                [f"{x.principal} -> {x.schema}.{x.table}" for x in a[:50]])
    if name == "least_privilege":
        adm = s["admin"]
        return ("attention" if adm else "pass",
                f"{len(adm)} principal(s) hold admin-level access." if adm
                else "No principals hold admin-level access.",
                [p.principal for p in adm[:50]])
    if name == "sod":
        sod = s["sod"]
        return ("attention" if sod else "pass",
                f"{len(sod)} principal(s) can both modify data and administer access." if sod
                else "No separation-of-duties conflicts detected.",
                [p.principal for p in sod[:50]])
    if name == "audit":
        return ("review",
                "Verify database/login auditing is enabled and retained — sqldoc "
                "cannot confirm audit configuration from schema metadata.", [])
    return ("review", "Manual review required.", [])


def assess(framework_id: str, ctx) -> FrameworkResult:
    if framework_id not in FRAMEWORKS:
        raise ValueError(f"Unknown framework '{framework_id}' (choose from {FRAMEWORK_CHOICES}).")
    name, controls = FRAMEWORKS[framework_id]
    s = _signals(ctx)
    result = FrameworkResult(framework=framework_id, name=name)
    for control_id, title, check in controls:
        status, detail, findings = _run_check(check, s)
        result.controls.append(ControlAssessment(
            control_id=control_id, title=title, status=status, detail=detail, findings=findings))
    return result


def assess_all(framework_ids, ctx) -> list:
    ids = list(framework_ids)
    if ids == ["all"] or "all" in ids:
        ids = FRAMEWORK_CHOICES
    return [assess(fid, ctx) for fid in ids]


def build_frameworks_json(results) -> dict:
    return {
        "report_type": "compliance-frameworks",
        "frameworks": [{
            "framework": r.framework, "name": r.name, "summary": r.summary,
            "controls": [{"control_id": c.control_id, "title": c.title, "status": c.status,
                          "detail": c.detail, "findings": c.findings} for c in r.controls],
        } for r in results],
    }
