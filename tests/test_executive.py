"""Executive summary: component scoring, top-risk ranking, trends, rendering."""
import os

from sqldoc import executive
from sqldoc.pii import Finding
from sqldoc.executive_renderer import render_executive_html


def _finding(risk, category="Email Address"):
    return Finding(schema="dbo", table="T", column="c", data_type="varchar",
                   category=category, risk=risk, confidence="x",
                   regulations=["GDPR"], action="", confidence_score=0.7)


class _Sec:
    def __init__(self, score, grade, high=0):
        self.score = score
        self.grade = grade
        self.findings = [type("F", (), {"severity": "HIGH"})() for _ in range(high)]


class _Backup:
    supported = True
    pitr_enabled = True

    def __init__(self, dbs):
        self.databases = dbs


class _DB:
    def __init__(self, never=False, issues=None):
        self.never_backed_up = never
        self.issues = issues or []


# --- component scoring ------------------------------------------------------

def test_health_score_penalizes_issues():
    assert executive.health_score(None) is None
    assert executive.health_score({"missing_indexes": 0}) == 100
    s = executive.health_score({"missing_indexes": 5, "slow_queries": 2})
    assert s == 100 - (5 * 4 + 2 * 3)


def test_health_score_floors_at_zero():
    assert executive.health_score({"missing_indexes": 100}) == 0


def test_backup_compliance():
    assert executive.backup_compliance(None) is None
    r = _Backup([_DB(), _DB(), _DB(never=True), _DB(issues=["stale"])])
    assert executive.backup_compliance(r) == 50   # 2 of 4 healthy


def test_backup_compliance_no_dbs():
    r = _Backup([])
    r.pitr_enabled = True
    assert executive.backup_compliance(r) == 100


def test_pii_risk_weights_high():
    assert executive.pii_risk(None) is None
    findings = [_finding("HIGH"), _finding("HIGH"), _finding("MEDIUM"), _finding("LOW")]
    assert executive.pii_risk(findings) == min(100, 8 * 2 + 3 + 1)


def test_pii_risk_caps_at_100():
    assert executive.pii_risk([_finding("HIGH")] * 50) == 100


# --- assembly + risks -------------------------------------------------------

def test_build_summary_overall_is_mean():
    findings = []  # pii_risk 0 -> safety 100
    summary = executive.build_summary(
        "DB", health_summary={"missing_indexes": 0}, findings=findings,
        backup_report=_Backup([_DB()]), security_report=_Sec(80, "B"))
    # components: health 100, backup 100, pii-safety 100, security 80 -> mean 95
    assert summary.overall_score == 95
    assert summary.overall_label == "Excellent"


def test_top_risks_ranked_backup_first():
    summary = executive.build_summary(
        "DB",
        findings=[_finding("HIGH")],
        backup_report=_Backup([_DB(never=True)]),
        security_report=_Sec(50, "D", high=2))
    assert len(summary.top_risks) <= 3
    # a never-backed-up database is the highest-weighted risk
    assert "no recent backup" in summary.top_risks[0]["title"]
    assert summary.top_risks[0]["severity"] == "Critical"


def test_no_risks_when_clean():
    summary = executive.build_summary(
        "DB", health_summary={"missing_indexes": 0}, findings=[],
        backup_report=_Backup([_DB()]), security_report=_Sec(100, "A"))
    assert summary.top_risks == []


def test_unavailable_sections_are_none():
    summary = executive.build_summary("DB", findings=[_finding("LOW")])
    assert summary.health_score is None
    assert summary.backup_compliance_pct is None
    assert summary.security_score is None
    assert summary.available == {"health": False, "pii": True, "backup": False, "security": False}


# --- trends -----------------------------------------------------------------

def test_trend_better_and_worse():
    prev = {"scores": {"overall_score": 60, "security_score": 90, "pii_risk_score": 10,
                       "health_score": None, "backup_compliance_pct": 100}}
    summary = executive.build_summary(
        "DB", health_summary={"missing_indexes": 0}, findings=[_finding("HIGH")],
        backup_report=_Backup([_DB()]), security_report=_Sec(70, "C"), previous=prev)
    # security dropped 90 -> 70 : worse
    assert summary.trends["security_score"]["better"] is False
    # pii_risk rose (10 -> 8) ... actually HIGH -> risk 8, down from 10 => better
    assert summary.trends["pii_risk_score"]["better"] is True


def test_snapshot_roundtrip():
    summary = executive.build_summary("DB", findings=[])
    snap = executive.to_snapshot(summary)
    assert snap["scores"]["overall_score"] == summary.overall_score


# --- rendering --------------------------------------------------------------

def test_render_executive_html(tmp_path):
    summary = executive.build_summary(
        "SalesDB", health_summary={"missing_indexes": 6}, findings=[_finding("HIGH")],
        backup_report=_Backup([_DB(never=True)]), security_report=_Sec(55, "D", high=1))
    out = tmp_path / "exec.html"
    render_executive_html(summary, str(out))
    html = out.read_text(encoding="utf-8")
    assert "Executive Summary" in html and "SalesDB" in html
    assert "Top priorities" in html
    # self-contained: no external resource references
    assert "http://" not in html and "https://" not in html
    assert "cdn" not in html.lower()


# --- CLI end-to-end (sqlite: health/infra off, so only PII runs) ------------

def test_executive_cli(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.extractor import Table, Column
    t = Table("main", "People", 5, columns=[
        Column("Id", "int", 4, False, True, False, None, None),
        Column("SSN", "varchar", 11, True, False, False, None, None)])
    monkeypatch.setattr(cli, "extract_metadata", lambda a: [t])
    out = tmp_path / "exec.html"
    res = CliRunner().invoke(cli.cli, [
        "executive", "--dialect", "sqlite", "--connection-string", str(tmp_path / "x.db"),
        "--database", "SalesDB", "--no-baseline", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert "Overall:" in res.output
    assert out.exists() and "SalesDB" in out.read_text(encoding="utf-8")


def test_render_all_clear(tmp_path):
    summary = executive.build_summary(
        "DB", health_summary={"missing_indexes": 0}, findings=[],
        backup_report=_Backup([_DB()]), security_report=_Sec(100, "A"))
    out = tmp_path / "e.html"
    render_executive_html(summary, str(out))
    assert "No urgent issues" in out.read_text(encoding="utf-8")
