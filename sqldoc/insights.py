"""AI-powered schema insights.

Four capabilities, exposed by the ``sqldoc insights`` command:

* **NL → SQL** — answer a plain-English question with a T-SQL query, grounded in
  the extracted schema (metadata only — names/types/keys, never row data).
* **Anomaly detection** — heuristic architectural smells: tables with no primary
  key, generic column names, missing audit columns, and name/type mismatches
  (e.g. a ``*Date`` column stored as ``varchar``).
* **Business glossary** — an AI-inferred business term + definition per table,
  producing a searchable glossary.
* **Relationship inference** — likely foreign keys between tables that have no
  explicit constraint, from column-name + PK-type matching, with a ready-to-run
  ``ALTER TABLE … ADD CONSTRAINT`` and a confidence score.

The AI parts (NL→SQL, glossary) go through :mod:`sqldoc.ai` and honour the
local/cloud mode switch; the heuristic parts (anomalies, relationships) need no
model and always run, so ``--no-ai`` still produces a useful report.
"""
import re
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

import sqldoc.ai as ai

STRING_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}

GENERIC_NAMES = {"data", "value", "value1", "value2", "temp", "tmp", "test", "col",
                 "col1", "column1", "field", "field1", "misc", "foo", "bar", "x", "y",
                 "z", "info", "stuff", "thing", "obj", "object", "item1"}
AUDIT_TOKENS = {"created", "createddate", "createdat", "datecreated", "createdon",
                "createdby", "modified", "modifieddate", "modifiedat", "updatedat",
                "updated", "updatedby", "lastmodified", "rowversion", "timestamp",
                "insertdate", "changedate"}

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _tokens(name):
    return [m.group(0).lower() for m in _CAMEL.finditer(name)]


def _compact(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _humanize(name):
    parts = [m.group(0) for m in _CAMEL.finditer(name)]
    return " ".join(parts) if parts else name


# --- dataclasses -----------------------------------------------------------

@dataclass
class Anomaly:
    severity: str          # HIGH / MEDIUM / LOW
    kind: str
    object: str
    detail: str
    recommendation: str


@dataclass
class GlossaryEntry:
    term: str
    category: str          # schema
    definition: str
    source: str            # schema.table


@dataclass
class RelationshipSuggestion:
    from_table: str        # schema.table
    from_column: str
    to_table: str          # schema.table
    to_column: str
    confidence: float
    reason: str
    ddl: str


@dataclass
class QueryResult:
    question: str
    sql: str


@dataclass
class InsightsReport:
    database: str
    anomalies: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    glossary: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# --- anomaly detection (heuristic) -----------------------------------------

_SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def detect_anomalies(tables) -> list:
    anomalies = []
    for t in tables:
        qual = f"{t.schema}.{t.name}"
        cols = t.columns

        if not any(c.is_primary_key for c in cols):
            anomalies.append(Anomaly(
                "HIGH", "no-primary-key", qual,
                "Table has no primary key.",
                "Add a primary key so rows are uniquely identifiable and replicable."))

        col_compacts = {_compact(c.name) for c in cols}
        if len(cols) >= 3 and not (col_compacts & AUDIT_TOKENS):
            anomalies.append(Anomaly(
                "LOW", "missing-audit-columns", qual,
                "No created/modified audit columns detected.",
                "Consider adding CreatedDate/ModifiedDate (and by-whom) for traceability."))

        for c in cols:
            comp = _compact(c.name)
            toks = set(_tokens(c.name))
            dt = (c.data_type or "").lower()

            if comp in GENERIC_NAMES:
                anomalies.append(Anomaly(
                    "MEDIUM", "generic-name", f"{qual}.{c.name}",
                    f"Column name '{c.name}' is generic and non-descriptive.",
                    "Rename to describe the business meaning of the value."))

            if ({"date", "time"} & toks or comp.endswith("date") or comp.endswith("time")) \
                    and dt in STRING_TYPES and not c.is_computed:
                anomalies.append(Anomaly(
                    "MEDIUM", "date-as-string", f"{qual}.{c.name}",
                    f"'{c.name}' looks like a date/time but is stored as {c.data_type}.",
                    "Use a date/datetime2 type to enable range queries and validation."))

            if ({"amount", "price", "cost", "total", "salary", "balance", "qty", "quantity"} & toks) \
                    and dt in STRING_TYPES and not c.is_computed:
                anomalies.append(Anomaly(
                    "MEDIUM", "number-as-string", f"{qual}.{c.name}",
                    f"'{c.name}' looks numeric but is stored as {c.data_type}.",
                    "Use a numeric/decimal/money type for correct arithmetic and sorting."))

            if (comp.startswith("is") or comp.startswith("has") or comp.endswith("flag")) \
                    and dt not in ("bit",) and len(comp) > 2 and not c.is_computed \
                    and dt in (STRING_TYPES | {"int", "smallint", "tinyint"}):
                anomalies.append(Anomaly(
                    "LOW", "bool-not-bit", f"{qual}.{c.name}",
                    f"'{c.name}' looks boolean but is stored as {c.data_type}.",
                    "Use the bit type for true/false flags."))

        if len(cols) > 45:
            anomalies.append(Anomaly(
                "LOW", "wide-table", qual,
                f"Table has {len(cols)} columns.",
                "Consider whether this table should be normalized into related tables."))

    anomalies.sort(key=lambda a: (_SEV_ORDER.get(a.severity, 3), a.object))
    return anomalies


# --- relationship inference (heuristic) ------------------------------------

_FK_COL = re.compile(r"^(?P<prefix>.+?)(?:id)$", re.IGNORECASE)


def _pk_index(tables):
    """Map lowercased (and de-pluralized) table name -> (table, single pk column)."""
    idx = {}
    for t in tables:
        pks = [c for c in t.columns if c.is_primary_key]
        if len(pks) != 1:
            continue
        low = t.name.lower()
        idx[low] = (t, pks[0])
        if low.endswith("s"):
            idx.setdefault(low[:-1], (t, pks[0]))
    return idx


def infer_relationships(tables) -> list:
    idx = _pk_index(tables)
    out = []
    for t in tables:
        for c in t.columns:
            if c.is_foreign_key or c.is_primary_key:
                continue
            m = _FK_COL.match(c.name)
            if not m:
                continue
            prefix = m.group("prefix")
            if not prefix or prefix.lower() == t.name.lower():
                continue
            hit = idx.get(prefix.lower()) or idx.get(prefix.lower() + "s")
            if not hit:
                continue
            ref_table, ref_pk = hit
            if ref_table.name == t.name:
                continue
            confidence, reasons = 0.6, ["column name implies a reference"]
            if (c.data_type or "").lower() == (ref_pk.data_type or "").lower():
                confidence += 0.25
                reasons.append("data type matches the referenced PK")
            if c.name.lower() == ref_pk.name.lower() or c.name.lower() == f"{ref_table.name}{ref_pk.name}".lower():
                confidence += 0.1
                reasons.append("column name matches the PK naming pattern")
            confidence = round(min(confidence, 0.99), 2)
            fk_name = f"FK_{t.name}_{c.name}"
            ddl = (f"ALTER TABLE [{t.schema}].[{t.name}] ADD CONSTRAINT [{fk_name}] "
                   f"FOREIGN KEY ([{c.name}]) REFERENCES [{ref_table.schema}].[{ref_table.name}] ([{ref_pk.name}]);")
            out.append(RelationshipSuggestion(
                from_table=f"{t.schema}.{t.name}", from_column=c.name,
                to_table=f"{ref_table.schema}.{ref_table.name}", to_column=ref_pk.name,
                confidence=confidence, reason="; ".join(reasons), ddl=ddl))
    out.sort(key=lambda r: r.confidence, reverse=True)
    return out


# --- AI: schema context, NL->SQL, glossary ---------------------------------

def _schema_context(tables, limit=60):
    lines = []
    for t in tables[:limit]:
        cols = ", ".join(
            f"{c.name} {c.data_type}"
            f"{' PK' if c.is_primary_key else ''}"
            f"{' FK->' + c.references_table if c.is_foreign_key and c.references_table else ''}"
            for c in t.columns
        )
        lines.append(f"{t.schema}.{t.name}({cols})")
    return "\n".join(lines)


def _ai_call(prompt, mode, model):
    return ai._call_ollama(prompt, model) if mode == "local" else ai._call_anthropic(prompt, model)


_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def answer_question(question, tables, mode="local", model="llama3.1:8b") -> QueryResult:
    schema = _schema_context(tables)
    prompt = (
        "You are a SQL Server expert. Using ONLY the schema below, write a single "
        "valid T-SQL query that answers the question. Do not invent tables or "
        "columns. Respond with only the SQL, no explanation.\n\n"
        f"Schema:\n{schema}\n\nQuestion: {question}\n\nSQL:"
    )
    text = _ai_call(prompt, mode, model).strip()
    m = _SQL_FENCE.search(text)
    sql = (m.group(1) if m else text).strip()
    return QueryResult(question=question, sql=sql)


def _glossary_for_table(t, mode, model) -> GlossaryEntry:
    col_names = ", ".join(c.name for c in t.columns[:25])
    prompt = (
        "In one sentence, give the business definition of a database table named "
        f"'{t.schema}.{t.name}' with columns: {col_names}. "
        "Respond with only the definition, no preamble."
    )
    definition = _ai_call(prompt, mode, model).strip()
    return GlossaryEntry(term=_humanize(t.name), category=t.schema,
                         definition=definition, source=f"{t.schema}.{t.name}")


def generate_glossary(tables, mode="local", model="llama3.1:8b", concurrency=8, errors=None) -> list:
    entries = []
    lock_errors = errors if errors is not None else []

    def work(t):
        try:
            return _glossary_for_table(t, mode, model)
        except Exception as e:
            lock_errors.append((f"glossary {t.schema}.{t.name}", f"{type(e).__name__}: {e}"))
            return None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for entry in pool.map(work, tables):
            if entry is not None:
                entries.append(entry)
    entries.sort(key=lambda g: (g.category, g.term))
    return entries


# --- orchestration ---------------------------------------------------------

def collect_insights(database, tables, questions=None, use_ai=True, glossary=True,
                     mode="local", model="llama3.1:8b", concurrency=8) -> InsightsReport:
    report = InsightsReport(database=database)
    report.anomalies = detect_anomalies(tables)
    report.relationships = infer_relationships(tables)

    if use_ai:
        for q in (questions or []):
            try:
                report.queries.append(answer_question(q, tables, mode, model))
            except Exception as e:
                report.errors.append((f"query: {q}", f"{type(e).__name__}: {e}"))
        if glossary:
            report.glossary = generate_glossary(tables, mode, model, concurrency, report.errors)
    return report


def summarize(report: InsightsReport) -> dict:
    by_sev = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for a in report.anomalies:
        by_sev[a.severity] = by_sev.get(a.severity, 0) + 1
    return {
        "anomalies": len(report.anomalies),
        "anomalies_by_severity": by_sev,
        "relationships": len(report.relationships),
        "glossary_terms": len(report.glossary),
        "queries": len(report.queries),
        "degraded": len(report.errors),
    }
