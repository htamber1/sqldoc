"""PII / compliance scanner.

Flags columns that likely hold personal or regulated data based on their name
and data type, maps each to the regulation(s) it implicates (HIPAA / GDPR /
PCI-DSS), and — optionally, with light data sampling — asks an LLM to confirm
whether sampled values actually look like PII. Sampled values are used only for
confidence scoring and are never stored or returned.
"""
import re
from dataclasses import dataclass, field, asdict

from sqldoc.extractor import get_connection, Table, Column
import sqldoc.ai as ai

STRING_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
NUMERIC_TYPES = {"int", "bigint", "smallint", "tinyint", "decimal", "numeric", "money", "smallmoney", "float", "real"}

RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


@dataclass
class PIICategory:
    name: str
    patterns: list          # regex fragments matched against the normalized column name
    severity: str           # base risk: HIGH / MEDIUM / LOW
    regulations: list
    action: str
    expected_types: set = field(default_factory=set)   # types that confirm; empty = any


# Ordered most-specific / most-sensitive first, so a column is attributed to its
# strongest match (e.g. "CreditCardNumber" -> Credit Card, not a generic number).
PII_CATEGORIES = [
    PIICategory("Payment Card", [r"creditcard", r"debitcard", r"cardnumber", r"\bcvv\b", r"\bcvc\b", r"cardexpir"],
                "HIGH", ["PCI-DSS"],
                "Never store the full PAN; tokenize or encrypt. Brings the system into PCI-DSS scope.",
                STRING_TYPES),
    PIICategory("National ID / SSN", [r"\bssn\b", r"socialsecurity", r"nationalid", r"taxid", r"\btin\b", r"aadhaar", r"\bnino\b", r"insurancenumber"],
                "HIGH", ["GDPR", "HIPAA"],
                "Encrypt at rest, enforce least-privilege access, and mask in non-production.",
                STRING_TYPES),
    PIICategory("Passport / Driver License", [r"passport", r"driverlicense", r"driverslicense", r"licensenumber"],
                "HIGH", ["GDPR"],
                "Encrypt, restrict access, and apply a retention/erasure policy.",
                STRING_TYPES),
    PIICategory("Bank Account", [r"\biban\b", r"bankaccount", r"accountnumber", r"routingnumber", r"sortcode", r"\bswift\b", r"\bbic\b"],
                "HIGH", ["PCI-DSS", "GDPR"],
                "Encrypt and restrict access; treat as regulated financial data.",
                STRING_TYPES),
    PIICategory("Health / Medical", [r"diagnosis", r"icd10", r"medicalrecord", r"healthplan", r"patientid", r"bloodtype", r"prescription", r"treatment", r"allerg", r"disability"],
                "HIGH", ["HIPAA", "GDPR"],
                "Protected Health Information: apply HIPAA safeguards, encryption, and access logging.",
                set()),
    PIICategory("Biometric", [r"biometric", r"fingerprint", r"faceid", r"faceprint", r"retina", r"irisscan", r"voiceprint", r"dnaprofile", r"dnasequence"],
                "HIGH", ["GDPR", "HIPAA"],
                "Biometric identifiers are GDPR special-category data: encrypt, minimize, and require explicit consent.",
                set()),
    PIICategory("Criminal Record", [r"criminalrecord", r"conviction", r"arrestrecord", r"offence", r"offense", r"probation"],
                "HIGH", ["GDPR"],
                "GDPR Article 10 data on criminal convictions/offences: restrict tightly and log all access.",
                set()),
    PIICategory("Credentials", [r"password", r"passwd", r"\bpwd\b", r"passwordhash", r"\bsecret\b", r"apikey", r"accesstoken", r"privatekey", r"\bsalt\b"],
                "HIGH", ["Security"],
                "Never store plaintext secrets; hash+salt passwords or use a secrets vault.",
                set()),
    PIICategory("Date of Birth", [r"dateofbirth", r"\bdob\b", r"birthdate", r"birthday"],
                "MEDIUM", ["GDPR", "HIPAA"],
                "Quasi-identifier; consider generalizing to an age band and restrict access.",
                DATE_TYPES | STRING_TYPES),
    PIICategory("Email Address", [r"email", r"emailaddress"],
                "MEDIUM", ["GDPR"],
                "Personal data under GDPR; collect with consent and minimize exposure.",
                STRING_TYPES),
    PIICategory("Phone Number", [r"\bphone", r"mobile", r"telephone", r"faxnumber", r"cellphone"],
                "MEDIUM", ["GDPR"],
                "Personal data; restrict access and consider masking in non-production.",
                STRING_TYPES),
    PIICategory("Postal Address", [r"addressline", r"streetaddress", r"postalcode", r"zipcode", r"\bzip\b"],
                "MEDIUM", ["GDPR"],
                "Personal data with geolocation risk; minimize retention.",
                STRING_TYPES),
    PIICategory("Special Category", [r"gender", r"\bsex\b", r"ethnicity", r"\brace\b", r"religion", r"nationality", r"maritalstatus", r"sexualorientation"],
                "MEDIUM", ["GDPR"],
                "GDPR special-category data; requires explicit consent and extra safeguards.",
                set()),
    PIICategory("Financial", [r"salary", r"compensation", r"annualincome", r"\bwage", r"networth"],
                "MEDIUM", ["GDPR"],
                "Sensitive personal/financial data; restrict access.",
                set()),
    PIICategory("Geolocation", [r"latitude", r"longitude", r"geolocation", r"gpscoord"],
                "MEDIUM", ["GDPR"],
                "Precise location is personal data under GDPR.",
                set()),
    PIICategory("Insurance / Policy", [r"policynumber", r"insuranceid", r"insurancepolicy", r"policyholder", r"claimnumber"],
                "MEDIUM", ["GDPR", "HIPAA"],
                "Links a person to coverage/claims; restrict access and treat as regulated where health-related.",
                set()),
    PIICategory("Vehicle / Registration", [r"licenseplate", r"numberplate", r"\bvin\b", r"vehicleregistration", r"registrationplate"],
                "MEDIUM", ["GDPR"],
                "A plate/VIN is an identifier linkable to a person; minimize retention.",
                STRING_TYPES),
    PIICategory("Full Name", [r"firstname", r"lastname", r"fullname", r"surname", r"givenname", r"middlename", r"forename", r"maidenname"],
                "LOW", ["GDPR"],
                "Personal data; minimize exposure and combine-with-other-fields risk.",
                STRING_TYPES),
    PIICategory("Online Identifier", [r"ipaddress", r"\bipaddr\b", r"username", r"userlogin", r"loginid", r"\blogin\b", r"screenname"],
                "LOW", ["GDPR"],
                "Online identifier linkable to a person under GDPR.",
                set()),
    PIICategory("Device Identifier", [r"macaddress", r"\bimei\b", r"\bimsi\b", r"deviceid", r"\budid\b", r"advertisingid"],
                "LOW", ["GDPR"],
                "A persistent device identifier can be linked to a person under GDPR.",
                set()),
    PIICategory("Age", [r"\bage\b", r"agegroup", r"ageband"],
                "LOW", ["GDPR"],
                "Quasi-identifier; combine-with-other-fields re-identification risk.",
                NUMERIC_TYPES | STRING_TYPES),
]


@dataclass
class Finding:
    schema: str
    table: str
    column: str
    data_type: str
    category: str
    risk: str                 # HIGH / MEDIUM / LOW (after confidence adjustment)
    confidence: str           # human-readable basis for the match
    regulations: list
    action: str
    confidence_score: float = 0.0   # 0.0-1.0, for --confidence-threshold filtering


# Split a column name into words, handling camelCase and acronym runs:
# "NationalIDNumber" -> national id number, "SSN" -> ssn, "DOB" -> dob.
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _tokens(name: str) -> str:
    return " ".join(m.group(0).lower() for m in _CAMEL.finditer(name))


def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _downgrade(risk: str) -> str:
    return {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}[risk]


def _match_category(column_name: str, categories: list = None):
    """Match a column name against a PII category list (defaults to the built-in
    catalog). Patterns containing a word boundary (\\b) are matched against the
    space-separated token string so short tokens (ssn, tin, dob, zip) don't
    over-match as substrings; other patterns match the separator-free compact
    name as substrings."""
    if categories is None:
        categories = PII_CATEGORIES
    toks = _tokens(column_name)
    compact = _compact(column_name)
    for cat in categories:
        for pat in cat.patterns:
            target = toks if r"\b" in pat else compact
            if re.search(pat, target):
                return cat
    return None


_VALID_SEVERITY = {"HIGH", "MEDIUM", "LOW"}


def load_custom_categories(items) -> list:
    """Parse enterprise-defined PII categories (from `.sqldoc.yml`'s
    `pii_patterns:`) into PIICategory objects. Raises ValueError on bad input."""
    cats = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            raise ValueError(f"pii_patterns[{i}] must be a mapping")
        name = it.get("category")
        patterns = it.get("patterns")
        if not name or not patterns:
            raise ValueError(f"pii_patterns[{i}] needs 'category' and a non-empty 'patterns' list")
        severity = str(it.get("severity", "MEDIUM")).upper()
        if severity not in _VALID_SEVERITY:
            raise ValueError(f"pii_patterns[{i}] severity must be HIGH/MEDIUM/LOW, got {severity!r}")
        cats.append(PIICategory(
            name=str(name),
            patterns=list(patterns),
            severity=severity,
            regulations=list(it.get("regulations", [])),
            action=str(it.get("action", "")),
            expected_types={str(t).lower() for t in it.get("types", [])},
        ))
    return cats


def scan_tables(tables, extra_categories=None) -> list:
    """Detect likely-PII columns from names + data types (no data access).
    `extra_categories` (custom, enterprise-defined) are checked before the
    built-in catalog so org-specific patterns take priority."""
    categories = list(extra_categories or []) + PII_CATEGORIES
    findings = []
    for t in tables:
        for col in t.columns:
            cat = _match_category(col.name, categories)
            if not cat:
                continue
            dtype = (col.data_type or "").lower()
            if cat.expected_types and dtype in cat.expected_types:
                risk, confidence, score = cat.severity, "name + type match", 0.9
            elif cat.expected_types and dtype and dtype not in cat.expected_types:
                # Only downgrade when a type is actually known to mismatch — DDL
                # parsed from a .sql file may carry no type, which is not evidence
                # against the name match.
                risk, confidence, score = _downgrade(cat.severity), "name match (type mismatch)", 0.4
            else:
                risk, confidence, score = cat.severity, "name match", 0.7
            findings.append(Finding(
                schema=t.schema, table=t.name, column=col.name, data_type=col.data_type,
                category=cat.name, risk=risk, confidence=confidence,
                regulations=list(cat.regulations), action=cat.action,
                confidence_score=score,
            ))
    return findings


def apply_allowlist(findings: list, patterns) -> tuple:
    """Drop findings for known-safe columns (an org allowlist from `.sqldoc.yml`
    `pii_allowlist:`). Each entry is matched case-insensitively with fnmatch
    globbing against the finding's ``schema.table.column``, ``table.column`` and
    bare ``column`` forms — so ``dbo.Users.Password``, ``Users.Password``,
    ``Password``, and ``dbo.*.Password`` all suppress it. Returns
    ``(kept, suppressed_count)``."""
    import fnmatch
    pats = [str(p).lower() for p in (patterns or []) if str(p).strip()]
    if not pats:
        return findings, 0
    kept, suppressed = [], 0
    for f in findings:
        candidates = [f"{f.schema}.{f.table}.{f.column}".lower(),
                      f"{f.table}.{f.column}".lower(), f.column.lower()]
        if any(fnmatch.fnmatch(c, p) for c in candidates for p in pats):
            suppressed += 1
        else:
            kept.append(f)
    return kept, suppressed


def filter_by_confidence(findings: list, threshold: float) -> tuple:
    """Drop findings whose confidence_score is below `threshold` (0.0-1.0).
    Returns ``(kept, dropped_count)``. A threshold of 0 keeps everything."""
    if not threshold:
        return findings, 0
    kept = [f for f in findings if f.confidence_score >= threshold]
    return kept, len(findings) - len(kept)


def _quote_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _sample_values(cursor, schema, table, column, limit=5):
    cursor.execute(
        f"SELECT TOP {int(limit)} {_quote_ident(column)} "
        f"FROM {_quote_ident(schema)}.{_quote_ident(table)} "
        f"WHERE {_quote_ident(column)} IS NOT NULL"
    )
    vals = []
    for row in cursor.fetchall():
        v = row[0]
        if v is None:
            continue
        vals.append(str(v)[:60])
    return vals


def _ai_confirm(category: str, values: list, mode: str, model: str) -> str:
    """Ask the LLM whether sampled values look like the suspected category.
    Returns 'YES' / 'NO' / 'UNSURE'. Values are used only here, never stored."""
    listing = "\n".join(f"  - {v}" for v in values)
    prompt = (
        f"You are a data-privacy scanner. Below are up to 5 sample values from a "
        f"database column suspected to contain {category} data.\n\nValues:\n{listing}\n\n"
        f"Do these values look like real {category} data? "
        f"Answer with exactly one word: YES, NO, or UNSURE."
    )
    text = (ai._call_ollama(prompt, model) if mode == "local" else ai._call_anthropic(prompt, model))
    token = re.sub(r"[^a-z]", "", text.strip().lower())[:6]
    if token.startswith("yes"):
        return "YES"
    if token.startswith("no"):
        return "NO"
    return "UNSURE"


def confirm_with_sampling(findings: list, connection_string: str, mode: str, model: str,
                          progress=None) -> list:
    """For each finding, sample up to 5 real values and let the AI confirm the
    category, adjusting risk/confidence. Sampled values are never stored."""
    conn = get_connection(connection_string)
    cursor = conn.cursor()
    try:
        for i, f in enumerate(findings):
            if progress:
                progress(i + 1, len(findings), f)
            try:
                values = _sample_values(cursor, f.schema, f.table, f.column)
            except Exception:
                continue
            if not values:
                f.confidence += "; no data to sample"
                continue
            verdict = _ai_confirm(f.category, values, mode, model)
            if verdict == "YES":
                f.risk = f.risk  # keep
                f.confidence = "AI-confirmed from sample"
                f.confidence_score = 0.97
            elif verdict == "NO":
                f.risk = _downgrade(_downgrade(f.risk))
                f.confidence = "AI: sampled values do not look like PII"
                f.confidence_score = 0.1
            else:
                f.confidence += "; AI unsure"
                f.confidence_score = min(f.confidence_score, 0.6)
    finally:
        conn.close()
    return findings


def findings_json(database: str, findings: list, sampled: bool = False) -> dict:
    """Machine-readable scan result: full findings + summary, for programmatic
    consumers (dashboards, data-catalog ingestion, CI parsing). Includes every
    Finding field; sampled values are never part of a Finding, so none leak."""
    from sqldoc import __version__
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "database": database,
        "sampled": bool(sampled),
        "summary": summarize(findings),
        "findings": [asdict(f) for f in findings],
    }


# --- Scanning DDL text (for the pre-commit hook) ---------------------------
# The pre-commit hook scans *staged .sql files* — migrations, DDL scripts —
# rather than a live database. We parse column definitions out of CREATE TABLE
# / ALTER TABLE ... ADD statements and run them through the same name-based
# matcher, so a developer who stages a column named `ssn` or `credit_card` is
# gated before the schema ever reaches a server. No database connection needed.

# leading identifier of a column definition, tolerating "quoted", `back-ticked`
# and [bracketed] names across dialects.
_COL_IDENT = re.compile(r'^\s*(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][\w$]*))')
# lines inside a CREATE TABLE body that are table constraints, not columns.
_CONSTRAINT_LEAD = re.compile(
    r'^\s*(?:CONSTRAINT|PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|KEY|INDEX|'
    r'FULLTEXT|SPATIAL|PERIOD|EXCLUDE)\b', re.IGNORECASE)
_CREATE_TABLE = re.compile(
    r'CREATE\s+(?:GLOBAL\s+|LOCAL\s+|TEMPORARY\s+|TEMP\s+|UNLOGGED\s+)*TABLE\s+'
    r'(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)\s*\(', re.IGNORECASE)
_ALTER_ADD = re.compile(
    r'ALTER\s+TABLE\s+([^\s]+)\s+ADD\s+(?:COLUMN\s+)?'
    r'(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][\w$]*))', re.IGNORECASE)


def _strip_sql_comments(text: str) -> str:
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.DOTALL)   # block comments
    text = re.sub(r'--[^\n]*', ' ', text)                     # line comments
    return text


def _unquote_ident(raw: str) -> str:
    return raw.strip().strip('"`[]').split('.')[-1]


def _split_top_level(body: str) -> list:
    """Split a CREATE TABLE body on top-level commas (ignoring commas nested in
    parentheses, e.g. decimal(10,2) or a nested constraint list)."""
    parts, depth, buf = [], 0, []
    for ch in body:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append(''.join(buf))
    return parts


def _table_body(text: str, open_paren_idx: int) -> str:
    """Return the substring inside the balanced parentheses whose opening paren
    is at open_paren_idx (which points *at* the '(')."""
    depth, start = 0, open_paren_idx
    for i in range(open_paren_idx, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return text[start + 1:]      # unbalanced — take the rest


def columns_from_sql(text: str) -> list:
    """Parse (table_name, column_name) pairs from DDL text — CREATE TABLE bodies
    and ALTER TABLE ... ADD statements. Best-effort and dialect-tolerant; used to
    feed the PII matcher from staged .sql files."""
    text = _strip_sql_comments(text)
    out = []
    for m in _CREATE_TABLE.finditer(text):
        table = _unquote_ident(m.group(1))
        body = _table_body(text, m.end() - 1)
        for part in _split_top_level(body):
            if not part.strip() or _CONSTRAINT_LEAD.match(part):
                continue
            cm = _COL_IDENT.match(part)
            if not cm:
                continue
            col = next(g for g in cm.groups() if g)
            out.append((table, col))
    for m in _ALTER_ADD.finditer(text):
        table = _unquote_ident(m.group(1))
        col = next(g for g in m.groups()[1:] if g)
        out.append((table, col))
    return out


def scan_sql_files(paths, extra_categories=None) -> list:
    """Run the PII matcher over the DDL in one or more .sql files. Columns are
    parsed from the file text (no database connection). Findings carry the file
    path in the `schema` slot so reports/allowlists can address them. Files that
    can't be read are skipped silently (a git hook shouldn't crash on a rename)."""
    tables_by_file = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        by_table = {}
        for table, col in columns_from_sql(text):
            by_table.setdefault(table, []).append(col)
        tabs = []
        for table, cols in by_table.items():
            tabs.append(Table(
                schema=path, name=table, row_count=0,
                columns=[Column(name=c, data_type="", max_length=None,
                                is_nullable=True, is_primary_key=False,
                                is_foreign_key=False, references_table=None,
                                references_column=None) for c in cols]))
        tables_by_file[path] = tabs
    findings = []
    for tabs in tables_by_file.values():
        findings.extend(scan_tables(tabs, extra_categories))
    return findings


def summarize(findings: list) -> dict:
    by_risk = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    by_reg = {}
    tables = set()
    for f in findings:
        by_risk[f.risk] = by_risk.get(f.risk, 0) + 1
        tables.add((f.schema, f.table))
        for r in f.regulations:
            by_reg[r] = by_reg.get(r, 0) + 1
    return {
        "total": len(findings),
        "by_risk": by_risk,
        "by_regulation": dict(sorted(by_reg.items(), key=lambda kv: -kv[1])),
        "tables_affected": len(tables),
    }


# --- PII drift detection ---------------------------------------------------
# Snapshot findings to JSON and diff two scans over time, like schema change
# detection but for regulated-data exposure.

PII_SNAPSHOT_VERSION = 1


def findings_snapshot(database: str, findings: list) -> dict:
    return {
        "version": PII_SNAPSHOT_VERSION,
        "database": database,
        "findings": {
            f"{f.schema}.{f.table}.{f.column}": {"risk": f.risk, "category": f.category}
            for f in findings
        },
    }


def diff_findings(old: dict, new: dict) -> dict:
    o = (old or {}).get("findings", {})
    n = new.get("findings", {})
    added = sorted(k for k in n if k not in o)
    resolved = sorted(k for k in o if k not in n)
    risk_changed = []
    for k in sorted(set(o) & set(n)):
        if o[k].get("risk") != n[k].get("risk"):
            risk_changed.append({
                "key": k, "old": o[k].get("risk"), "new": n[k].get("risk"),
                "category": n[k].get("category"),
            })
    diff = {"added": added, "resolved": resolved, "risk_changed": risk_changed, "_new": n}
    diff["counts"] = {"added": len(added), "resolved": len(resolved), "changed": len(risk_changed)}
    diff["has_changes"] = bool(added or resolved or risk_changed)
    return diff


def iter_findings_diff_lines(diff: dict):
    """Yield (kind, text) for terminal rendering. kind in:
    new / resolved / escalate / deescalate / summary / none."""
    if not diff["has_changes"]:
        yield ("none", "No PII drift since the last scan.")
        return
    new = diff.get("_new", {})
    for k in diff["added"]:
        info = new.get(k, {})
        yield ("new", f"+ NEW       {info.get('risk', ''):6} {k}  ({info.get('category', '')})")
    for k in diff["resolved"]:
        yield ("resolved", f"- RESOLVED  {k}")
    for ch in diff["risk_changed"]:
        up = RISK_ORDER.get(ch["new"], 0) > RISK_ORDER.get(ch["old"], 0)
        yield ("escalate" if up else "deescalate",
               f"! RISK {'UP  ' if up else 'DOWN'} {ch['key']}: {ch['old']} -> {ch['new']}")
    c = diff["counts"]
    yield ("summary", f"PII drift: {c['added']} new, {c['resolved']} resolved, {c['changed']} risk change(s)")


def format_findings_diff(diff: dict) -> str:
    return "\n".join(text for _, text in iter_findings_diff_lines(diff))
