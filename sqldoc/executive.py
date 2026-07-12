"""Executive summary — a single-page, non-technical health/risk report.

`sqldoc executive` aggregates the deep technical commands (health, PII scan,
backup, security) into four plain-English scores and a "top 3 things to fix"
list aimed at a CTO / CISO — no DMV names, no jargon. Trend arrows compare the
current run to a stored snapshot so leadership can see whether things are getting
better or worse over time.

The scoring + risk-ranking here is pure (it takes already-collected sub-reports),
so it is fully unit-testable without a database. The CLI collects each
sub-report (guarding on dialect capability) and hands them in; any that aren't
available on the dialect are passed as None and simply omitted.
"""
from dataclasses import dataclass, field, asdict


@dataclass
class Risk:
    title: str          # plain-English headline
    detail: str         # one sentence of context
    severity: str       # Critical / High / Medium
    _weight: int = 0    # internal ranking weight (higher = more urgent)


@dataclass
class ExecutiveSummary:
    database: str
    generated_label: str = ""
    overall_score: int = 0
    overall_label: str = ""
    health_score: int = None
    pii_risk_score: int = None
    pii_safety_score: int = None
    backup_compliance_pct: int = None
    security_score: int = None
    security_grade: str = ""
    top_risks: list = field(default_factory=list)
    trends: dict = field(default_factory=dict)     # metric -> {delta, direction, better}
    available: dict = field(default_factory=dict)  # which sections had data


# --- component scoring ------------------------------------------------------

def health_score(health_summary) -> int:
    """0-100 (higher = healthier) from the health command's issue counts. Each
    open issue costs points; heavier categories cost more."""
    if not health_summary:
        return None
    weighted = (
        health_summary.get("missing_indexes", 0) * 4
        + health_summary.get("slow_queries", 0) * 3
        + health_summary.get("fragmented_indexes", 0) * 2
        + health_summary.get("dead_tables", 0) * 2
        + health_summary.get("unused_procedures", 0) * 1
        + health_summary.get("duplicate_tables", 0) * 2
        + health_summary.get("redundant_indexes", 0) * 1
    )
    return max(0, 100 - min(100, weighted))


def backup_compliance(backup_report) -> int:
    """Percentage of databases with an acceptable, non-stale backup posture."""
    if backup_report is None or not getattr(backup_report, "supported", True):
        return None
    dbs = backup_report.databases
    if not dbs:
        return 100 if backup_report.pitr_enabled else 0
    healthy = sum(1 for d in dbs if not d.never_backed_up and not d.issues)
    return round(100 * healthy / len(dbs))


def pii_risk(findings) -> int:
    """0-100 (higher = MORE exposed), severity-weighted (HIGH dominates)."""
    if findings is None:
        return None
    high = sum(1 for f in findings if f.risk == "HIGH")
    medium = sum(1 for f in findings if f.risk == "MEDIUM")
    low = sum(1 for f in findings if f.risk == "LOW")
    return int(min(100, round(high * 8 + medium * 3 + low * 1)))


# --- plain-English labels ---------------------------------------------------

def score_label(score) -> str:
    if score is None:
        return "Not measured"
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 60:
        return "Fair"
    if score >= 40:
        return "Needs attention"
    return "Urgent"


def _mean_int(values) -> int:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals)) if vals else 0


# --- top-risk ranking -------------------------------------------------------

def _collect_risks(health_summary, findings, backup_report, security_report) -> list:
    risks = []

    if findings is not None:
        high = sum(1 for f in findings if f.risk == "HIGH")
        if high:
            risks.append(Risk(
                title=f"{high} column{'s' if high != 1 else ''} expose highly sensitive personal data",
                detail="These hold regulated information (such as identifiers, health, or "
                       "payment data) and should be reviewed for encryption and access limits.",
                severity="Critical", _weight=90 + high))

    if security_report is not None:
        sev_high = sum(1 for f in security_report.findings if f.severity == "HIGH")
        if sev_high:
            risks.append(Risk(
                title=f"{sev_high} serious security weakness{'es' if sev_high != 1 else ''} found",
                detail="Configuration or access settings leave the database more exposed than "
                       "it should be. These are typically quick for the team to correct.",
                severity="Critical", _weight=85 + sev_high))

    if backup_report is not None and getattr(backup_report, "supported", True):
        bad = [d for d in backup_report.databases if d.never_backed_up or d.issues]
        never = [d for d in backup_report.databases if d.never_backed_up]
        if never:
            risks.append(Risk(
                title=f"{len(never)} database{'s' if len(never) != 1 else ''} ha{'ve' if len(never) != 1 else 's'} no recent backup",
                detail="If something failed today, recent data could not be recovered. "
                       "Restoring a reliable backup schedule is the highest-value fix.",
                severity="Critical", _weight=95 + len(never)))
        elif bad:
            risks.append(Risk(
                title=f"{len(bad)} database{'s' if len(bad) != 1 else ''} ha{'ve' if len(bad) != 1 else 's'} a backup concern",
                detail="Backups exist but are stale or incomplete; the recovery window is "
                       "wider than recommended.",
                severity="High", _weight=70 + len(bad)))

    if health_summary:
        perf = (health_summary.get("missing_indexes", 0)
                + health_summary.get("slow_queries", 0)
                + health_summary.get("fragmented_indexes", 0))
        if perf >= 3:
            risks.append(Risk(
                title="Database performance is being held back",
                detail="Several queries are slower than they need to be. Addressing them "
                       "improves application responsiveness without new hardware.",
                severity="Medium", _weight=40 + min(perf, 30)))

    risks.sort(key=lambda r: r._weight, reverse=True)
    return risks


# --- assembly ---------------------------------------------------------------

_TREND_METRICS = ("overall_score", "health_score", "pii_risk_score",
                  "backup_compliance_pct", "security_score")
# For these, a lower number is BETTER (more exposed = worse).
_LOWER_IS_BETTER = {"pii_risk_score"}


def _trend(metric, current, previous) -> dict:
    if current is None or previous is None:
        return None
    delta = current - previous
    if delta == 0:
        return {"delta": 0, "direction": "flat", "better": None}
    up = delta > 0
    better = (not up) if metric in _LOWER_IS_BETTER else up
    return {"delta": delta, "direction": "up" if up else "down", "better": better}


def build_summary(database, health_summary=None, findings=None, backup_report=None,
                  security_report=None, previous=None, generated_label="") -> ExecutiveSummary:
    """Assemble the executive summary from whichever sub-reports are available."""
    hs = health_score(health_summary)
    bc = backup_compliance(backup_report)
    pr = pii_risk(findings)
    ps = None if pr is None else max(0, 100 - pr)
    sec = security_report.score if security_report is not None else None
    grade = security_report.grade if security_report is not None else ""

    overall = _mean_int([hs, bc, ps, sec])
    risks = _collect_risks(health_summary, findings, backup_report, security_report)

    summary = ExecutiveSummary(
        database=database,
        generated_label=generated_label,
        overall_score=overall,
        overall_label=score_label(overall),
        health_score=hs,
        pii_risk_score=pr,
        pii_safety_score=ps,
        backup_compliance_pct=bc,
        security_score=sec,
        security_grade=grade,
        top_risks=[{"title": r.title, "detail": r.detail, "severity": r.severity}
                   for r in risks[:3]],
        available={
            "health": hs is not None,
            "pii": pr is not None,
            "backup": bc is not None,
            "security": sec is not None,
        },
    )

    # Trends vs the previous run's stored scores.
    current_scores = {
        "overall_score": overall, "health_score": hs, "pii_risk_score": pr,
        "backup_compliance_pct": bc, "security_score": sec,
    }
    if previous:
        prev_scores = previous.get("scores", {})
        for m in _TREND_METRICS:
            t = _trend(m, current_scores.get(m), prev_scores.get(m))
            if t is not None:
                summary.trends[m] = t
    return summary


def to_snapshot(summary) -> dict:
    """Serialize the scores for trend comparison on the next run."""
    return {
        "schema_version": 1,
        "database": summary.database,
        "scores": {
            "overall_score": summary.overall_score,
            "health_score": summary.health_score,
            "pii_risk_score": summary.pii_risk_score,
            "backup_compliance_pct": summary.backup_compliance_pct,
            "security_score": summary.security_score,
        },
    }


def build_executive_json(summary) -> dict:
    return {"report_type": "executive", **asdict(summary)}
