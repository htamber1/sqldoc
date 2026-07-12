"""Industry / vertical tuning: profiles, PII escalation, AI-prompt guidance."""
import pytest

from sqldoc import ai, industry
from sqldoc.pii import Finding


def _finding(category, risk="MEDIUM", regs=None):
    return Finding(schema="dbo", table="Patients", column="c", data_type="varchar",
                   category=category, risk=risk, confidence="name match",
                   regulations=list(regs or []), action="", confidence_score=0.7)


# --- profiles ---------------------------------------------------------------

def test_all_four_industries_present():
    assert set(industry.INDUSTRY_CHOICES) == {"healthcare", "finance", "retail", "government"}


def test_get_industry_case_insensitive():
    assert industry.get_industry("HEALTHCARE").key == "healthcare"


def test_get_industry_none():
    assert industry.get_industry(None) is None
    assert industry.get_industry("") is None


def test_get_industry_unknown_raises():
    with pytest.raises(ValueError):
        industry.get_industry("aerospace")


def test_focus_regulations():
    assert industry.get_industry("healthcare").focus_regulations == ("HIPAA",)
    assert "SOX" in industry.get_industry("finance").focus_regulations
    assert "FedRAMP" in industry.get_industry("government").focus_regulations


def test_guidance_for():
    assert "HIPAA" in industry.guidance_for("healthcare")
    assert industry.guidance_for(None) == ""


# --- PII escalation ---------------------------------------------------------

def test_healthcare_escalates_dob_to_high():
    prof = industry.get_industry("healthcare")
    findings = [_finding("Date of Birth", "MEDIUM")]
    industry.apply_to_findings(findings, prof)
    assert findings[0].risk == "HIGH"
    assert "HIPAA" in findings[0].regulations
    assert "Healthcare escalation" in findings[0].confidence


def test_escalation_caps_at_high():
    prof = industry.get_industry("healthcare")
    findings = [_finding("Health / Medical", "HIGH")]
    industry.apply_to_findings(findings, prof)
    assert findings[0].risk == "HIGH"          # already HIGH, stays HIGH


def test_non_sensitive_category_untouched():
    prof = industry.get_industry("healthcare")
    findings = [_finding("Vehicle / Registration", "LOW", ["GDPR"])]
    industry.apply_to_findings(findings, prof)
    assert findings[0].risk == "LOW"
    assert findings[0].regulations == ["GDPR"]


def test_finance_adds_sox_to_payment_card():
    prof = industry.get_industry("finance")
    findings = [_finding("Payment Card", "HIGH", ["PCI-DSS"])]
    industry.apply_to_findings(findings, prof)
    assert "SOX" in findings[0].regulations


def test_apply_none_profile_is_noop():
    findings = [_finding("Date of Birth", "MEDIUM")]
    industry.apply_to_findings(findings, None)
    assert findings[0].risk == "MEDIUM"


# --- AI guidance is prepended by dispatch -----------------------------------

def test_dispatch_prepends_industry_guidance(monkeypatch):
    ai.set_backend(None)
    ai.set_industry_guidance(industry.guidance_for("healthcare"))
    seen = {}
    monkeypatch.setattr(ai, "_call_ollama", lambda p, m: seen.setdefault("p", p) or "x")
    try:
        ai.dispatch("Describe table Patients.", mode="local")
    finally:
        ai.set_industry_guidance("")
    assert "HIPAA" in seen["p"] and "Describe table Patients." in seen["p"]
