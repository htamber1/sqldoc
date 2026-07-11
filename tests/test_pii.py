"""PII / compliance detection engine."""
import pytest

from sqldoc.pii import (scan_tables, summarize, _match_category, Finding,
                        apply_allowlist, filter_by_confidence)
from sqldoc.extractor import Table, Column


def _col(name, dtype="nvarchar"):
    return Column(name, dtype, 50, True, False, False, None, None)


def _table(cols):
    return Table("dbo", "T", 1, columns=cols)


@pytest.mark.parametrize("name,category", [
    ("SSN", "National ID / SSN"),
    ("NationalIDNumber", "National ID / SSN"),
    ("CreditCardNumber", "Payment Card"),
    ("CardNumber", "Payment Card"),
    ("EmailAddress", "Email Address"),
    ("PhoneNumber", "Phone Number"),
    ("DateOfBirth", "Date of Birth"),
    ("BirthDate", "Date of Birth"),
    ("PassportNumber", "Passport / Driver License"),
    ("PasswordHash", "Credentials"),
    ("PostalCode", "Postal Address"),
    ("Gender", "Special Category"),
])
def test_detects_common_pii(name, category):
    m = _match_category(name)
    assert m is not None and m.name == category, (name, m and m.name)


@pytest.mark.parametrize("name,category", [
    ("Fingerprint", "Biometric"),
    ("DNAProfile", "Biometric"),
    ("CriminalRecord", "Criminal Record"),
    ("PolicyNumber", "Insurance / Policy"),
    ("LicensePlate", "Vehicle / Registration"),
    ("MacAddress", "Device Identifier"),
    ("IMEI", "Device Identifier"),
    ("Age", "Age"),
])
def test_detects_extended_pii(name, category):
    m = _match_category(name)
    assert m is not None and m.name == category, (name, m and m.name)


@pytest.mark.parametrize("name", [
    "Rating", "DiscontinuedDate", "AdditionalContactInfo",
    "ProductID", "Quantity", "ModifiedDate", "ProductName",
    # extended-category false positives: none of these should match "Age"/"Vehicle"/etc.
    "Usage", "PageCount", "MessageId", "Language", "ProvinceId", "Vintage",
])
def test_avoids_false_positives(name):
    assert _match_category(name) is None, name


def test_confidence_scores():
    hi = scan_tables([_table([_col("EmailAddress", "nvarchar")])])[0]
    assert hi.confidence_score == 0.9                    # name + type
    mismatch = scan_tables([_table([_col("NationalID", "int")])])[0]
    assert mismatch.confidence_score == 0.4              # type mismatch


def test_apply_allowlist_forms():
    findings = scan_tables([_table([_col("EmailAddress"), _col("NationalID")])])
    kept, n = apply_allowlist(findings, ["dbo.T.EmailAddress"])   # full path
    assert n == 1 and all(f.column != "EmailAddress" for f in kept)
    assert apply_allowlist(findings, ["nationalid"])[1] == 1      # bare column, case-insensitive
    assert apply_allowlist(findings, ["dbo.*.Email*"])[1] == 1    # glob
    assert apply_allowlist(findings, [])[1] == 0                   # empty = no-op


def test_filter_by_confidence():
    weak = scan_tables([_table([_col("NationalID", "int")])])      # 0.4
    kept, dropped = filter_by_confidence(weak, 0.5)
    assert dropped == 1 and kept == []
    strong = scan_tables([_table([_col("EmailAddress")])])         # 0.9
    assert filter_by_confidence(strong, 0.5)[1] == 0
    assert filter_by_confidence(strong, 0.0)[1] == 0               # threshold 0 = keep all


def test_type_match_boosts_confidence():
    f = scan_tables([_table([_col("EmailAddress", "nvarchar")])])[0]
    assert f.risk == "MEDIUM"
    assert "type match" in f.confidence


def test_type_mismatch_downgrades_risk():
    # National ID is normally HIGH, but as an int the type contradicts -> MEDIUM
    f = scan_tables([_table([_col("NationalID", "int")])])[0]
    assert f.risk == "MEDIUM"
    assert "type mismatch" in f.confidence


def test_regulation_mapping():
    card = scan_tables([_table([_col("CardNumber")])])[0]
    assert "PCI-DSS" in card.regulations
    nid = scan_tables([_table([_col("NationalIDNumber")])])[0]
    assert {"GDPR", "HIPAA"} <= set(nid.regulations)


def test_summary_counts_and_regs():
    findings = scan_tables([_table([
        _col("CardNumber"), _col("EmailAddress"), _col("FirstName"), _col("ProductName"),
    ])])
    s = summarize(findings)
    assert s["total"] == 3                    # ProductName is not PII
    assert s["by_risk"]["HIGH"] >= 1          # CardNumber
    assert "GDPR" in s["by_regulation"]


def test_finding_never_stores_sampled_values():
    fields = set(Finding.__dataclass_fields__)
    assert not (fields & {"sample", "samples", "values"})


# --- PII drift detection ---------------------------------------------------

def _snap(names_risks):
    from sqldoc.pii import findings_snapshot
    fs = [Finding("dbo", "T", name, "nvarchar", "Cat", risk, "conf", ["GDPR"], "act")
          for name, risk in names_risks]
    return findings_snapshot("DB", fs)


def test_findings_diff_detects_new_resolved_and_risk_change():
    from sqldoc.pii import diff_findings
    old = _snap([("Email", "MEDIUM"), ("SSN", "HIGH"), ("OldCol", "LOW")])
    new = _snap([("Email", "MEDIUM"), ("SSN", "LOW"), ("NewCol", "HIGH")])
    d = diff_findings(old, new)
    assert d["added"] == ["dbo.T.NewCol"]
    assert d["resolved"] == ["dbo.T.OldCol"]
    assert len(d["risk_changed"]) == 1
    ch = d["risk_changed"][0]
    assert ch["key"] == "dbo.T.SSN" and ch["old"] == "HIGH" and ch["new"] == "LOW"
    assert d["has_changes"] is True


def test_findings_diff_no_change():
    from sqldoc.pii import diff_findings, format_findings_diff
    s = _snap([("Email", "MEDIUM")])
    d = diff_findings(s, s)
    assert d["has_changes"] is False
    assert "No PII drift" in format_findings_diff(d)


# --- Custom PII patterns ---------------------------------------------------

def test_custom_category_detects_new_column():
    from sqldoc.pii import load_custom_categories
    cats = load_custom_categories([{
        "category": "Employee Number", "patterns": ["employeenumber"],
        "severity": "MEDIUM", "regulations": ["Internal"], "action": "Restrict.",
        "types": ["int"],
    }])
    t = _table([_col("EmployeeNumber", "int")])
    f = scan_tables([t], extra_categories=cats)[0]
    assert f.category == "Employee Number"
    assert f.risk == "MEDIUM" and "Internal" in f.regulations


def test_custom_category_takes_priority_over_builtin():
    from sqldoc.pii import load_custom_categories
    cats = load_custom_categories([{
        "category": "Internal Account", "patterns": ["accountnumber"], "severity": "LOW",
    }])
    f = scan_tables([_table([_col("AccountNumber", "varchar")])], extra_categories=cats)[0]
    assert f.category == "Internal Account"   # not the built-in "Bank Account"


def test_load_custom_categories_validation():
    from sqldoc.pii import load_custom_categories
    with pytest.raises(ValueError):
        load_custom_categories([{"category": "X"}])                       # no patterns
    with pytest.raises(ValueError):
        load_custom_categories([{"category": "X", "patterns": ["a"], "severity": "BOGUS"}])
    with pytest.raises(ValueError):
        load_custom_categories(["not-a-mapping"])
