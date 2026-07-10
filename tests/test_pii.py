"""PII / compliance detection engine."""
import pytest

from sqldoc.pii import scan_tables, summarize, _match_category, Finding
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


@pytest.mark.parametrize("name", [
    "Rating", "DiscontinuedDate", "AdditionalContactInfo",
    "ProductID", "Quantity", "ModifiedDate", "ProductName",
])
def test_avoids_false_positives(name):
    assert _match_category(name) is None, name


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
