"""Collect a database once, then feed every integration from the same bundle.

``gather()`` runs the read-only collection pass (schema extract, PII scan, and —
where the dialect supports them — health, backup, and security), capability-gated
and best-effort exactly like ``sqldoc executive``. From the resulting
:class:`Bundle` we derive:

* uploadable :class:`~sqldoc.integrations.base.Artifact` reports (HTML/PDF/JSON)
  via :func:`render_artifacts`;
* a flat metrics dict (:func:`metrics`) for Power BI / dashboards;
* actionable :class:`~sqldoc.integrations.base.FindingEvent` s (:func:`finding_events`)
  for issue trackers (Jira / ServiceNow).

Collecting once and fanning out keeps a `--push` to several destinations from
re-scanning the database per destination.
"""
import os
import tempfile
from dataclasses import dataclass, field

from sqldoc.integrations.base import Artifact, FindingEvent

# Default report kinds an integration uploads when the caller doesn't narrow it.
DEFAULT_KINDS = ("doc_html", "executive_html", "pii_html", "pii_json", "health_json", "metrics_json")


@dataclass
class Bundle:
    database: str
    tables: list = field(default_factory=list)
    views: list = field(default_factory=list)
    procedures: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    health_summary: dict = None
    backup_report: object = None
    security_report: object = None
    executive_summary: object = None
    notes: list = field(default_factory=list)   # human-readable "section skipped" notes


def gather(adapter, database, schemas=None, industry=None) -> Bundle:
    """Run the read-only collection pass against ``adapter``. Each optional
    section is capability-gated and best-effort: a failure appends a note and
    leaves that piece empty rather than aborting the whole push."""
    from sqldoc.pii import scan_tables
    from sqldoc.health import collect_health, summarize as health_summarize
    from sqldoc.backup import collect_backups
    from sqldoc.secure import collect_security
    from sqldoc import executive as executive_mod
    from sqldoc.snapshot import load_snapshot, save_snapshot

    b = Bundle(database=database)
    b.tables = adapter.extract_metadata()
    try:
        b.views = adapter.extract_views()
    except Exception as e:
        b.notes.append(f"views not collected: {e}")
    try:
        b.procedures = adapter.extract_procedures()
    except Exception as e:
        b.notes.append(f"procedures not collected: {e}")

    if schemas:
        allow = [s.strip() for s in schemas.split(',')] if isinstance(schemas, str) else list(schemas)
        b.tables = [t for t in b.tables if t.schema in allow]
        b.views = [v for v in b.views if v.schema in allow]
        b.procedures = [p for p in b.procedures if p.schema in allow]

    b.findings = scan_tables(b.tables)
    if industry:
        try:
            from sqldoc import industry as industry_mod
            industry_mod.apply_to_findings(b.findings, industry)
        except Exception as e:
            b.notes.append(f"industry tuning skipped: {e}")

    caps = adapter.capabilities
    if getattr(caps, "health", False):
        try:
            b.health_summary = health_summarize(collect_health(adapter))
        except Exception as e:
            b.notes.append(f"health skipped: {e}")
    if getattr(caps, "infra_monitoring", False):
        try:
            b.backup_report = collect_backups(adapter)
        except Exception as e:
            b.notes.append(f"backup skipped: {e}")
        try:
            b.security_report = collect_security(adapter)
        except Exception as e:
            b.notes.append(f"security skipped: {e}")

    try:
        b.executive_summary = executive_mod.build_summary(
            database, health_summary=b.health_summary, findings=b.findings,
            backup_report=b.backup_report, security_report=b.security_report,
            previous=None, generated_label=f"Generated for {database}.")
    except Exception as e:
        b.notes.append(f"executive summary skipped: {e}")

    return b


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def render_artifacts(bundle: Bundle, kinds=None) -> list:
    """Render the requested report kinds from an already-collected bundle to
    in-memory :class:`Artifact` s (via a scratch dir the renderers write to)."""
    from sqldoc.renderer import render_html
    from sqldoc.pdf_renderer import render_pdf
    from sqldoc.executive_renderer import render_executive_html
    from sqldoc.pii_renderer import render_pii_html
    from sqldoc.health_renderer import build_health_json
    from sqldoc.pii import summarize as pii_summarize

    kinds = list(kinds or DEFAULT_KINDS)
    db = bundle.database
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in db) or "database"
    out = []
    import json as _json

    with tempfile.TemporaryDirectory(prefix="sqldoc-reports-") as tmp:
        def _p(fn):
            return os.path.join(tmp, fn)

        if "doc_html" in kinds:
            p = _p(f"{safe}-doc.html")
            render_html(db, bundle.tables, p, bundle.views, bundle.procedures)
            out.append(Artifact(f"{safe}-doc.html", "doc_html", _read(p), "text/html"))

        if "doc_pdf" in kinds:
            p = _p(f"{safe}-doc.pdf")
            render_pdf(db, bundle.tables, p, bundle.views, bundle.procedures)
            out.append(Artifact(f"{safe}-doc.pdf", "doc_pdf", _read(p), "application/pdf"))

        if "executive_html" in kinds and bundle.executive_summary is not None:
            p = _p(f"{safe}-executive.html")
            render_executive_html(bundle.executive_summary, p)
            out.append(Artifact(f"{safe}-executive.html", "executive_html", _read(p), "text/html"))

        if "pii_html" in kinds:
            p = _p(f"{safe}-pii.html")
            render_pii_html(db, bundle.findings, p)
            out.append(Artifact(f"{safe}-pii.html", "pii_html", _read(p), "text/html"))

        if "pii_json" in kinds:
            payload = {
                "database": db,
                "summary": pii_summarize(bundle.findings),
                "findings": [_finding_dict(f) for f in bundle.findings],
            }
            data = _json.dumps(payload, indent=2, default=str).encode("utf-8")
            out.append(Artifact(f"{safe}-pii.json", "pii_json", data, "application/json"))

        if "health_json" in kinds and bundle.health_summary is not None:
            data = _json.dumps({"database": db, "health": bundle.health_summary},
                               indent=2, default=str).encode("utf-8")
            out.append(Artifact(f"{safe}-health.json", "health_json", data, "application/json"))

        if "metrics_json" in kinds:
            data = _json.dumps(metrics(bundle), indent=2, default=str).encode("utf-8")
            out.append(Artifact(f"{safe}-metrics.json", "metrics_json", data, "application/json"))

    return out


def _finding_dict(f) -> dict:
    return {
        "schema": f.schema, "table": f.table, "column": f.column,
        "data_type": f.data_type, "category": f.category, "risk": f.risk,
        "confidence": getattr(f, "confidence", None),
        "regulations": list(getattr(f, "regulations", []) or []),
        "action": getattr(f, "action", None),
    }


def metrics(bundle: Bundle) -> dict:
    """Flat scalar metrics for streaming dashboards / property maps. Missing
    scores are ``None`` (the dialect didn't support that section)."""
    from sqldoc.pii import summarize as pii_summarize
    s = bundle.executive_summary
    pii = pii_summarize(bundle.findings)
    high = pii.get("by_risk", {}).get("HIGH") if isinstance(pii.get("by_risk"), dict) else None
    if high is None:
        high = sum(1 for f in bundle.findings if f.risk == "HIGH")
    return {
        "database": bundle.database,
        "tables": len(bundle.tables),
        "pii_findings": len(bundle.findings),
        "pii_high": high,
        "pii_safety_score": getattr(s, "pii_safety_score", None) if s else None,
        "backup_compliance_pct": getattr(s, "backup_compliance_pct", None) if s else None,
        "security_score": getattr(s, "security_score", None) if s else None,
        "security_grade": getattr(s, "security_grade", None) if s else None,
        "health_score": getattr(s, "health_score", None) if s else None,
        "overall_score": getattr(s, "overall_score", None) if s else None,
    }


def finding_events(bundle: Bundle, thresholds: dict = None) -> list:
    """Turn a bundle into actionable :class:`FindingEvent` s for issue trackers.

    Thresholds (all optional): ``pii_high`` (>=1 HIGH PII finding fires by
    default), ``security_min`` (fire when the security score is below it),
    ``health_min`` (fire when the performance score is below it),
    ``backup_max_age_hours`` (fire on stale/never backups)."""
    thresholds = thresholds or {}
    db = bundle.database
    events = []

    high = [f for f in bundle.findings if f.risk == "HIGH"]
    if high:
        regs = sorted({r for f in high for r in (getattr(f, "regulations", []) or [])})
        cols = ", ".join(f"{f.schema}.{f.table}.{f.column}" for f in high[:20])
        events.append(FindingEvent(
            kind="pii", severity="high",
            title=f"[sqldoc] {len(high)} HIGH-risk PII column(s) in {db}",
            detail=f"Regulations: {', '.join(regs) or 'n/a'}.\nColumns: {cols}"
                   + ("" if len(high) <= 20 else f"\n(+{len(high) - 20} more)"),
            database=db, fields={"count": len(high), "regulations": regs}))

    s = bundle.executive_summary
    sec_min = thresholds.get("security_min")
    sec = getattr(s, "security_score", None) if s else None
    if sec_min is not None and sec is not None and sec < sec_min:
        events.append(FindingEvent(
            kind="security", severity="high" if sec < sec_min - 15 else "medium",
            title=f"[sqldoc] Security score {sec}/100 below threshold ({sec_min}) on {db}",
            detail=f"The unified security scan scored {sec}/100 "
                   f"(grade {getattr(s, 'security_grade', '?')}), under the configured "
                   f"minimum of {sec_min}.",
            database=db, fields={"score": sec, "threshold": sec_min}))

    health_min = thresholds.get("health_min")
    hs = getattr(s, "health_score", None) if s else None
    if health_min is not None and hs is not None and hs < health_min:
        events.append(FindingEvent(
            kind="health", severity="medium",
            title=f"[sqldoc] Performance/health score {hs}/100 below threshold ({health_min}) on {db}",
            detail=f"The health/DMV analysis scored {hs}/100, under the configured "
                   f"minimum of {health_min}. Review slow queries, missing indexes, "
                   f"and fragmentation in the health report.",
            database=db, fields={"score": hs, "threshold": health_min}))

    # Backup staleness — surfaced by the executive summary as a top risk, or by a
    # backup_compliance_pct below 100.
    bpct = getattr(s, "backup_compliance_pct", None) if s else None
    if bpct is not None and bpct < 100:
        events.append(FindingEvent(
            kind="backup", severity="medium" if bpct >= 50 else "high",
            title=f"[sqldoc] Backup coverage {bpct}% on {db}",
            detail=f"Backup compliance is {bpct}% — one or more databases are stale or "
                   f"have never been backed up. See the backup section of the report.",
            database=db, fields={"backup_compliance_pct": bpct}))

    return events
