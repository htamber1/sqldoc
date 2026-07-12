"""Industry / vertical tuning.

`--industry {healthcare,finance,retail,government}` tunes three things at once:

  1. **AI descriptions** — a vertical-specific guidance paragraph is appended to
     every table/column/view/procedure prompt so the model reads the schema
     through the right lens (PHI for healthcare, cardholder data for finance,
     records-retention for government, ...).
  2. **PII sensitivity** — categories most sensitive to that vertical are
     *escalated* one risk level (e.g. in healthcare any Date of Birth / Full
     Name / Email becomes part of a PHI record, so it is treated as HIGH), and
     the vertical's flagship regulation is added to each escalated finding.
  3. **Compliance focus** — `focus_regulations` drives which regulation section
     the `comply` report leads with and which controls it emphasises.

The module is pure data + small helpers so it is trivially testable and has no
database or network dependency.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class IndustryProfile:
    key: str
    label: str
    ai_guidance: str                       # appended to AI description prompts
    focus_regulations: tuple               # regs the compliance report leads with
    escalate_categories: frozenset         # PII category names bumped one level
    added_regulation: str                  # reg tagged onto escalated findings
    pii_note: str                          # one-line banner note
    compliance_focus: str                  # one-line comply-report framing


# --- risk escalation --------------------------------------------------------
_ORDER = ["LOW", "MEDIUM", "HIGH"]


def _escalate(risk: str) -> str:
    try:
        i = _ORDER.index(risk)
    except ValueError:
        return risk
    return _ORDER[min(i + 1, len(_ORDER) - 1)]


INDUSTRIES = {
    "healthcare": IndustryProfile(
        key="healthcare",
        label="Healthcare",
        ai_guidance=(
            "CONTEXT: This database belongs to a healthcare organization subject to HIPAA. "
            "When describing each object, note whether it may hold Protected Health "
            "Information (PHI) — patient identifiers, demographics, diagnoses, procedures, "
            "medications, insurance, or provider notes — and flag any column that links a "
            "person to a health condition."),
        focus_regulations=("HIPAA",),
        # Under HIPAA, common identifiers become PHI once tied to a patient record.
        escalate_categories=frozenset({
            "Health / Medical", "Full Name", "Date of Birth", "Email Address",
            "Phone Number", "Postal Address", "National ID / SSN", "Insurance / Policy",
            "Geolocation", "Age"}),
        added_regulation="HIPAA",
        pii_note="Healthcare mode: patient identifiers are treated as PHI (escalated one risk level).",
        compliance_focus="Leads with HIPAA PHI safeguards (Privacy + Security Rule controls).",
    ),
    "finance": IndustryProfile(
        key="finance",
        label="Financial Services",
        ai_guidance=(
            "CONTEXT: This database belongs to a financial-services organization subject to "
            "PCI-DSS (cardholder data) and SOX (financial-reporting integrity). When "
            "describing each object, note whether it may hold cardholder data, account or "
            "transaction records, or data material to financial reporting and audit."),
        focus_regulations=("PCI-DSS", "SOX"),
        escalate_categories=frozenset({
            "Payment Card", "Bank Account", "Financial", "National ID / SSN",
            "Full Name", "Postal Address"}),
        added_regulation="SOX",
        pii_note="Finance mode: cardholder + account data escalated; SOX financial-record flag added.",
        compliance_focus="Leads with PCI-DSS cardholder-data controls and SOX audit-trail integrity.",
    ),
    "retail": IndustryProfile(
        key="retail",
        label="Retail / E-commerce",
        ai_guidance=(
            "CONTEXT: This database belongs to a retail / e-commerce organization handling "
            "consumer PII (GDPR/CCPA) and payment data (PCI-DSS). When describing each "
            "object, note whether it may hold customer profiles, orders, payment details, "
            "marketing consent, or behavioural / loyalty data."),
        focus_regulations=("PCI-DSS", "GDPR"),
        escalate_categories=frozenset({
            "Payment Card", "Email Address", "Phone Number", "Postal Address",
            "Geolocation", "Online Identifier", "Device Identifier"}),
        added_regulation="GDPR",
        pii_note="Retail mode: consumer contact + payment + tracking identifiers escalated (GDPR/PCI-DSS).",
        compliance_focus="Leads with PCI-DSS payment controls and GDPR consumer-consent / data-subject rights.",
    ),
    "government": IndustryProfile(
        key="government",
        label="Government / Public Sector",
        ai_guidance=(
            "CONTEXT: This database belongs to a government / public-sector organization "
            "subject to FedRAMP controls and statutory records-retention requirements. When "
            "describing each object, note whether it holds citizen / constituent records, "
            "national identifiers, case files, or data with a defined retention schedule, and "
            "whether it would fall under a records-management or FOIA obligation."),
        focus_regulations=("FedRAMP", "GDPR"),
        escalate_categories=frozenset({
            "National ID / SSN", "Passport / Driver License", "Full Name",
            "Postal Address", "Date of Birth", "Criminal Record", "Biometric",
            "Geolocation"}),
        added_regulation="FedRAMP",
        pii_note="Government mode: national identifiers + case data escalated (FedRAMP + records-retention focus).",
        compliance_focus="Leads with FedRAMP access controls and statutory data-retention / records management.",
    ),
}

INDUSTRY_CHOICES = tuple(INDUSTRIES.keys())


def get_industry(key):
    """Return the IndustryProfile for key, or None if key is falsy. Raises
    ValueError on an unrecognized key."""
    if not key:
        return None
    prof = INDUSTRIES.get(str(key).lower())
    if prof is None:
        raise ValueError(
            f"Unknown industry '{key}' (choose from {', '.join(INDUSTRY_CHOICES)}).")
    return prof


def guidance_for(key):
    """The AI-prompt guidance string for an industry key, or '' if none."""
    prof = get_industry(key)
    return prof.ai_guidance if prof else ""


def apply_to_findings(findings, profile):
    """Escalate the risk of findings whose category is sensitive to the industry
    and tag them with the industry's flagship regulation. Mutates + returns the
    findings list. A no-op when profile is None."""
    if profile is None:
        return findings
    for f in findings:
        if f.category in profile.escalate_categories:
            new_risk = _escalate(f.risk)
            if new_risk != f.risk:
                f.risk = new_risk
                f.confidence = f.confidence + f" (+{profile.label} escalation)"
            if profile.added_regulation not in f.regulations:
                f.regulations = list(f.regulations) + [profile.added_regulation]
    return findings
