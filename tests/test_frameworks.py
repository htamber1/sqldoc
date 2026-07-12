"""Compliance-framework assessment mapped to control numbers."""
import pytest

from sqldoc import frameworks as fw
from sqldoc.frameworks import assess, assess_all, build_frameworks_json, FRAMEWORK_CHOICES
from sqldoc.frameworks_renderer import render_frameworks_html
from sqldoc.pii import Finding
from sqldoc.comply import PrincipalAccess, AccessAlert


def _ctx(pii_high=True, admin=True, sod=True, alerts=True):
    findings = [Finding("Sales", "Customer", "Email", "varchar", "email",
                        "MEDIUM", "name", ["GDPR"], "Encrypt")]
    if pii_high:
        findings.append(Finding("Sales", "Customer", "SSN", "varchar", "government_id",
                                "HIGH", "name", ["HIPAA"], "Encrypt"))
    principals = []
    if admin:
        principals.append(PrincipalAccess(principal="CORP\\Admin", levels=["read", "admin"]))
    if sod:
        principals.append(PrincipalAccess(principal="CORP\\Bob", levels=["write", "admin"]))
    access_alerts = []
    if alerts:
        access_alerts.append(AccessAlert(principal="CORP\\App", permission="SELECT",
                                         schema="Sales", table="Customer", max_risk="HIGH"))
    return {"pii_findings": findings, "principals": principals, "access_alerts": access_alerts,
            "permissions": []}


# --- catalog ---------------------------------------------------------------

def test_all_frameworks_present():
    assert set(FRAMEWORK_CHOICES) == {"sox", "fedramp", "iso27001", "cmmc", "ccpa", "pipeda", "soc2"}


@pytest.mark.parametrize("fid", FRAMEWORK_CHOICES)
def test_each_framework_assesses(fid):
    r = assess(fid, _ctx())
    assert r.controls and all(c.control_id and c.status in ("attention", "review", "pass")
                              for c in r.controls)


def test_unknown_framework():
    with pytest.raises(ValueError):
        assess("hipaa2", _ctx())


# --- signal mapping --------------------------------------------------------

def test_sox_sod_flagged():
    r = assess("sox", _ctx(sod=True))
    sod = next(c for c in r.controls if c.control_id == "Section-404")
    assert sod.status == "attention" and sod.findings == ["CORP\\Bob"]


def test_fedramp_least_privilege_attention():
    r = assess("fedramp", _ctx(admin=True))
    ac6 = next(c for c in r.controls if c.control_id == "AC-6")
    assert ac6.status == "attention" and "CORP\\Admin" in ac6.findings


def test_ccpa_data_inventory():
    r = assess("ccpa", _ctx())
    inv = next(c for c in r.controls if c.control_id == "1798.100")
    assert inv.status == "attention" and "Sales.Customer" in inv.findings


def test_audit_control_always_review():
    r = assess("soc2", _ctx())
    audit = next(c for c in r.controls if c.control_id == "CC7.2")
    assert audit.status == "review"


def test_clean_context_passes():
    clean = {"pii_findings": [], "principals": [], "access_alerts": [], "permissions": []}
    r = assess("iso27001", clean)
    # only the audit control stays 'review'; the rest pass
    statuses = {c.status for c in r.controls}
    assert "attention" not in statuses
    assert any(c.status == "pass" for c in r.controls)


# --- assess_all + json + render --------------------------------------------

def test_assess_all_expands_all():
    results = assess_all(["all"], _ctx())
    assert len(results) == len(FRAMEWORK_CHOICES)


def test_build_frameworks_json():
    j = build_frameworks_json(assess_all(["sox", "soc2"], _ctx()))
    assert j["report_type"] == "compliance-frameworks" and len(j["frameworks"]) == 2
    assert j["frameworks"][0]["controls"]


def test_render_frameworks_html_offline(tmp_path):
    from sqldoc.offline import verify_file
    out = tmp_path / "fw.html"
    render_frameworks_html(assess_all(["sox", "fedramp", "ccpa"], _ctx()), "TestDB", str(out))
    text = out.read_text(encoding="utf-8")
    assert "SOX" in text and "Section-404" in text and "FedRAMP" in text
    assert verify_file(str(out)) == []


# --- CLI integration -------------------------------------------------------

def test_cli_comply_frameworks(monkeypatch, tmp_path, sample_tables):
    from click.testing import CliRunner
    from sqldoc import cli
    from conftest import build_tables, build_views, build_procs
    monkeypatch.setattr(cli, "extract_metadata", lambda a: build_tables())
    monkeypatch.setattr(cli, "extract_views", lambda a: build_views())
    monkeypatch.setattr(cli, "extract_procedures", lambda a: build_procs())
    out = tmp_path / "comply.html"
    res = CliRunner().invoke(cli.comply, [
        "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-access-audit", "--output", str(out), "--frameworks", "sox,soc2"])
    assert res.exit_code == 0, res.output
    assert "SOX" in res.output
    assert (tmp_path / "comply-frameworks.html").exists()
