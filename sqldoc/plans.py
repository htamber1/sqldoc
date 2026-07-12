"""Query execution-plan analyzer across dialects.

Pulls the worst-performing cached queries and — on SQL Server — parses their XML
execution plans to flag well-known anti-patterns (table scans on large tables,
key/RID lookups, implicit conversions, missing-index recommendations, and
sort/hash spills to tempdb). An LLM then explains, per plan, why it is slow and
exactly what index or rewrite would fix it.

* **SQL Server** — ``sys.dm_exec_query_stats`` + ``sys.dm_exec_query_plan`` +
  ``sys.dm_exec_sql_text`` (the practical "top-N worst cached plans" join), with
  full XML-plan pattern analysis.
* **PostgreSQL** — ``pg_stat_statements`` (query text + timings; no plan XML).
* **MySQL** — ``performance_schema.events_statements_summary_by_digest``
  (with the ``SUM_NO_INDEX_USED`` heuristic).

The AI receives the query text + detected patterns — necessary to give a useful
fix — but never table row data.
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import sqldoc.ai as ai
from sqldoc.dbutil import cell

PLAN_DIALECTS = {"sqlserver", "azuresql", "azure_managed_instance", "postgres", "mysql"}
_LARGE_ROWS = 10000.0


def _s(v) -> str:
    return "" if v is None else str(v)


def _f(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


@dataclass
class PlanPattern:
    kind: str            # table-scan / key-lookup / implicit-conversion / missing-index / spill / no-index
    severity: str        # HIGH / MEDIUM / LOW
    detail: str
    count: int = 1


@dataclass
class QueryPlan:
    query_text: str
    avg_elapsed_ms: float = 0.0
    executions: int = 0
    total_elapsed_ms: float = 0.0
    avg_reads: int = 0
    patterns: list = field(default_factory=list)      # PlanPattern
    ai_explanation: str = ""

    @property
    def severity(self) -> str:
        if any(p.severity == "HIGH" for p in self.patterns):
            return "HIGH"
        if any(p.severity == "MEDIUM" for p in self.patterns):
            return "MEDIUM"
        return "LOW"


@dataclass
class PlansReport:
    dialect: str = ""
    supported: bool = True
    plans: list = field(default_factory=list)         # QueryPlan
    has_plan_xml: bool = False                        # SQL Server only
    notes: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# --- SQL Server XML plan analysis ------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}")[-1]


def parse_plan_xml(plan_xml: str, large_rows: float = _LARGE_ROWS) -> list:
    """Detect anti-patterns in a SQL Server showplan XML string."""
    found = []
    if not plan_xml:
        return found
    try:
        root = ET.fromstring(plan_xml)
    except ET.ParseError:
        return found

    def add(kind, severity, detail):
        found.append(PlanPattern(kind, severity, detail))

    for el in root.iter():
        lt = _local(el.tag)
        if lt == "MissingIndexGroup":
            impact = el.get("Impact", "")
            add("missing-index", "HIGH",
                f"Optimizer wants a missing index (estimated impact {impact}%).")
        elif lt == "RelOp":
            op = el.get("PhysicalOp", "")
            rows = _f(el.get("EstimateRows", "0"))
            if op == "Table Scan":
                add("table-scan", "HIGH" if rows > large_rows else "MEDIUM",
                    f"Table Scan (~{int(rows):,} rows) — no usable index (heap).")
            elif op in ("Clustered Index Scan", "Index Scan") and rows > large_rows:
                add("large-scan", "MEDIUM", f"{op} over ~{int(rows):,} rows — consider a more selective index.")
            elif op in ("Key Lookup", "Key Lookup (Clustered)", "RID Lookup", "RID Lookup (Heap)"):
                add("key-lookup", "MEDIUM", "Key/RID lookup — add the SELECTed columns to a covering index.")
        # Only the optimizer's PlanAffectingConvert warning is flagged: a bare
        # <Convert Implicit="1"> appears in almost every plan for benign reasons.
        elif lt == "PlanAffectingConvert":
            add("implicit-conversion", "HIGH",
                "Plan-affecting implicit conversion — an index seek was downgraded to a scan.")
        elif lt in ("SpillToTempDb", "SortWarning", "HashWarning"):
            add("spill", "HIGH", "Operator spilled to tempdb (insufficient memory grant / bad estimate).")

    # Deduplicate identical patterns, keeping a count.
    merged = {}
    for p in found:
        key = (p.kind, p.detail)
        if key in merged:
            merged[key].count += 1
        else:
            merged[key] = p
    ordered = sorted(merged.values(), key=lambda p: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[p.severity])
    return ordered


def _collect_sqlserver(cursor, top: int) -> PlansReport:
    report = PlansReport(dialect="sqlserver", has_plan_xml=True)
    cursor.execute(f"""
        SELECT TOP ({int(top)})
            (qs.total_elapsed_time / qs.execution_count) / 1000.0 AS avg_elapsed_ms,
            qs.execution_count,
            qs.total_elapsed_time / 1000.0 AS total_elapsed_ms,
            qs.total_logical_reads / qs.execution_count AS avg_reads,
            SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
                ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                  ELSE qs.statement_end_offset END - qs.statement_start_offset)/2)+1) AS query_text,
            CAST(qp.query_plan AS nvarchar(max)) AS plan_xml
        FROM sys.dm_exec_query_stats qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
        CROSS APPLY sys.dm_exec_query_plan(qs.plan_handle) qp
        ORDER BY qs.total_elapsed_time DESC
    """)
    for r in cursor.fetchall():
        plan = QueryPlan(
            query_text=_collapse_ws(_s(cell(r, "query_text")))[:800],
            avg_elapsed_ms=_f(cell(r, "avg_elapsed_ms")),
            executions=_i(cell(r, "execution_count")),
            total_elapsed_ms=_f(cell(r, "total_elapsed_ms")),
            avg_reads=_i(cell(r, "avg_reads")),
            patterns=parse_plan_xml(_s(cell(r, "plan_xml"))),
        )
        report.plans.append(plan)
    return report


# --- PostgreSQL ------------------------------------------------------------

def _collect_postgres(cursor, top: int) -> PlansReport:
    report = PlansReport(dialect="postgres")
    cursor.execute(f"""
        SELECT query, calls,
               total_exec_time AS total_ms,
               mean_exec_time AS avg_ms,
               shared_blks_read + shared_blks_hit AS avg_reads
        FROM pg_stat_statements
        ORDER BY total_exec_time DESC
        LIMIT {int(top)}
    """)
    for r in cursor.fetchall():
        report.plans.append(QueryPlan(
            query_text=_collapse_ws(_s(cell(r, "query")))[:800],
            avg_elapsed_ms=_f(cell(r, "avg_ms")),
            executions=_i(cell(r, "calls")),
            total_elapsed_ms=_f(cell(r, "total_ms")),
            avg_reads=_i(cell(r, "avg_reads")),
        ))
    report.notes.append("PostgreSQL does not expose plan XML in the catalog — "
                        "patterns come from the query + AI. Run EXPLAIN for a full plan.")
    return report


# --- MySQL -----------------------------------------------------------------

def _collect_mysql(cursor, top: int) -> PlansReport:
    report = PlansReport(dialect="mysql")
    cursor.execute(f"""
        SELECT DIGEST_TEXT AS query, COUNT_STAR AS calls,
               SUM_TIMER_WAIT / 1e9 AS total_ms, AVG_TIMER_WAIT / 1e9 AS avg_ms,
               SUM_ROWS_EXAMINED AS rows_examined, SUM_NO_INDEX_USED AS no_index_used
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST_TEXT IS NOT NULL
        ORDER BY SUM_TIMER_WAIT DESC
        LIMIT {int(top)}
    """)
    for r in cursor.fetchall():
        plan = QueryPlan(
            query_text=_collapse_ws(_s(cell(r, "query")))[:800],
            avg_elapsed_ms=_f(cell(r, "avg_ms")),
            executions=_i(cell(r, "calls")),
            total_elapsed_ms=_f(cell(r, "total_ms")),
            avg_reads=_i(cell(r, "rows_examined")),
        )
        if _i(cell(r, "no_index_used")):
            plan.patterns.append(PlanPattern("no-index", "HIGH",
                                             "Ran without using any index (full scan)."))
        report.plans.append(plan)
    return report


# --- dispatch --------------------------------------------------------------

def collect_plans(adapter, top: int = 20) -> PlansReport:
    dialect = getattr(adapter, "dialect", "sqlserver")
    if dialect not in PLAN_DIALECTS:
        return PlansReport(dialect=dialect, supported=False,
                           errors=[("Unsupported", f"Plan analysis is not implemented for {dialect}.")])
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        if dialect in ("sqlserver", "azuresql", "azure_managed_instance"):
            return _collect_sqlserver(cursor, top)
        if dialect == "postgres":
            return _collect_postgres(cursor, top)
        return _collect_mysql(cursor, top)
    finally:
        conn.close()


# --- AI explanation --------------------------------------------------------

def _ai_call(prompt, mode, model):
    return ai.dispatch(prompt, mode, model, max_tokens=600).strip()


def explain_plan(plan: QueryPlan, dialect: str, mode: str = "local", model: str = None) -> str:
    pats = "; ".join(f"{p.kind} ({p.severity})" for p in plan.patterns) or "no specific pattern detected"
    prompt = (
        f"You are a {dialect} query-tuning expert. This query averages "
        f"{plan.avg_elapsed_ms} ms over {plan.executions} executions. Detected plan "
        f"issues: {pats}.\n\nSQL:\n{plan.query_text}\n\n"
        "Explain briefly why it is slow, then give the exact fix: the specific "
        "CREATE INDEX statement (with columns and INCLUDE) or the query rewrite. "
        "Be concrete. Under 150 words.")
    return _ai_call(prompt, mode, model).strip()


def explain_plans(report: PlansReport, mode: str = "local", model: str = None, limit: int = 5):
    """Explain the top `limit` plans in place."""
    for plan in report.plans[:limit]:
        try:
            plan.ai_explanation = explain_plan(plan, report.dialect, mode, model)
        except Exception:
            pass


def summarize(report: PlansReport) -> dict:
    plans = report.plans
    kinds = {}
    for p in plans:
        for pat in p.patterns:
            kinds[pat.kind] = kinds.get(pat.kind, 0) + 1
    return {
        "plans": len(plans),
        "high_severity": sum(1 for p in plans if p.severity == "HIGH"),
        "pattern_counts": kinds,
        "has_plan_xml": report.has_plan_xml,
        "worst_ms": max((p.avg_elapsed_ms for p in plans), default=0),
    }
