"""Data-quality analysis: null rates, per-column distribution, and duplicate
records.

Unlike the documentation/scan/health paths, this reads **actual table data** —
but only in *aggregate* (COUNT / COUNT DISTINCT / MIN / MAX / GROUP BY). The one
place raw values surface is each column's most-frequent values (for the
distribution view); those are truncated and stay local — nothing is sent to any
AI or network. Every per-column query is isolated so one failure (e.g. an
un-groupable large-object column) records an error and the rest proceed.
"""
from dataclasses import dataclass, field

from sqldoc.extractor import get_connection

STRING_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
COMPARABLE_TYPES = {"int", "bigint", "smallint", "tinyint", "decimal", "numeric",
                    "money", "smallmoney", "float", "real",
                    "date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
# Types that cannot be GROUP BY'd / COUNT(DISTINCT)'d directly.
UNGROUPABLE_TYPES = {"text", "ntext", "image", "xml", "geography", "geometry",
                     "hierarchyid", "sql_variant"}


@dataclass
class ColumnQuality:
    schema: str
    table: str
    column: str
    data_type: str
    total_rows: int
    null_count: int
    null_rate: float                 # 0.0-1.0
    distinct_count: int              # -1 when not computable (large-object type)
    blank_count: int                 # empty/whitespace-only strings (string types)
    min_value: str
    max_value: str
    top_values: list = field(default_factory=list)   # [{value, count}]

    @property
    def distinct_rate(self) -> float:
        non_null = self.total_rows - self.null_count
        if non_null <= 0 or self.distinct_count < 0:
            return 0.0
        return self.distinct_count / non_null

    @property
    def is_constant(self) -> bool:
        """Only one distinct value across a non-empty column — often dead weight."""
        return self.total_rows > 0 and self.distinct_count == 1

    @property
    def flags(self) -> list:
        f = []
        if self.total_rows and self.null_rate >= 0.5:
            f.append("high-null")
        if self.is_constant:
            f.append("constant")
        if self.blank_count:
            f.append("blanks")
        return f


@dataclass
class DuplicateGroup:
    schema: str
    table: str
    columns_considered: list
    duplicate_groups: int            # distinct key combos appearing more than once
    duplicate_rows: int              # redundant rows (sum(count) - groups)


@dataclass
class QualityReport:
    database: str
    columns: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _quote_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _first(cursor):
    rows = cursor.fetchall()
    return rows[0] if rows else None


def analyze_column_quality(cursor, schema, table, column, data_type, top_values=5) -> ColumnQuality:
    tbl = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    col = _quote_ident(column)
    dt = (data_type or "").lower()
    groupable = dt not in UNGROUPABLE_TYPES

    distinct_expr = f"COUNT(DISTINCT {col})" if groupable else "-1"
    blank_expr = (f"SUM(CASE WHEN LTRIM(RTRIM({col})) = '' THEN 1 ELSE 0 END)"
                  if dt in STRING_TYPES else "0")
    if dt in COMPARABLE_TYPES:
        min_expr = f"CONVERT(varchar(64), MIN({col}))"
        max_expr = f"CONVERT(varchar(64), MAX({col}))"
    else:
        min_expr = max_expr = "NULL"

    cursor.execute(
        f"SELECT COUNT(*) AS total, COUNT({col}) AS non_null, "
        f"{distinct_expr} AS distinct_count, {blank_expr} AS blank_count, "
        f"{min_expr} AS min_val, {max_expr} AS max_val FROM {tbl}"
    )
    r = _first(cursor)
    if r is None:
        return None
    total = int(r.total or 0)
    non_null = int(r.non_null or 0)
    null_count = total - non_null

    top = []
    if groupable and top_values and non_null > 0:
        cursor.execute(
            f"SELECT TOP ({int(top_values)}) {col} AS val, COUNT(*) AS freq "
            f"FROM {tbl} WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY COUNT(*) DESC"
        )
        for row in cursor.fetchall():
            top.append({"value": ("" if row.val is None else str(row.val))[:80],
                        "count": int(row.freq or 0)})

    return ColumnQuality(
        schema=schema, table=table, column=column, data_type=data_type,
        total_rows=total, null_count=null_count,
        null_rate=round(null_count / total, 4) if total else 0.0,
        distinct_count=int(r.distinct_count) if r.distinct_count is not None else -1,
        blank_count=int(r.blank_count or 0),
        min_value="" if r.min_val is None else str(r.min_val),
        max_value="" if r.max_val is None else str(r.max_val),
        top_values=top,
    )


def detect_duplicates(cursor, schema, table, columns) -> DuplicateGroup:
    """Full-row duplicate detection: group by every groupable, non-computed
    column and count combinations that appear more than once."""
    groupable = [c for c in columns
                 if not c.is_computed and (c.data_type or "").lower() not in UNGROUPABLE_TYPES]
    if not groupable:
        return None
    tbl = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    collist = ", ".join(_quote_ident(c.name) for c in groupable)
    cursor.execute(
        f"SELECT ISNULL(SUM(cnt), 0) AS dup_rows, COUNT(*) AS dup_groups FROM ("
        f"SELECT COUNT(*) AS cnt FROM {tbl} GROUP BY {collist} HAVING COUNT(*) > 1) g"
    )
    r = _first(cursor)
    if r is None:
        return None
    groups = int(r.dup_groups or 0)
    total_in_groups = int(r.dup_rows or 0)
    if groups == 0:
        return None
    return DuplicateGroup(
        schema=schema, table=table, columns_considered=[c.name for c in groupable],
        duplicate_groups=groups, duplicate_rows=total_in_groups - groups,
    )


def collect_quality(connection_string, tables, top_values=5, schemas=None,
                    detect_dupes=True, progress=None) -> QualityReport:
    report = QualityReport(database="")
    allow = set(schemas) if schemas else None
    conn = get_connection(connection_string)
    cursor = conn.cursor()
    try:
        targets = [t for t in tables if allow is None or t.schema in allow]
        for i, t in enumerate(targets):
            if progress:
                progress(i + 1, len(targets), t)
            for col in t.columns:
                if col.is_computed:
                    continue
                try:
                    cq = analyze_column_quality(cursor, t.schema, t.name, col.name,
                                                col.data_type, top_values=top_values)
                    if cq is not None:
                        report.columns.append(cq)
                except Exception as e:
                    report.errors.append((f"{t.schema}.{t.name}.{col.name}",
                                          f"{type(e).__name__}: {e}"))
            if detect_dupes:
                try:
                    dg = detect_duplicates(cursor, t.schema, t.name, t.columns)
                    if dg is not None:
                        report.duplicates.append(dg)
                except Exception as e:
                    report.errors.append((f"{t.schema}.{t.name} (duplicates)",
                                          f"{type(e).__name__}: {e}"))
    finally:
        conn.close()
    return report


def summarize(report: QualityReport) -> dict:
    high_null = sum(1 for c in report.columns if c.total_rows and c.null_rate >= 0.5)
    constant = sum(1 for c in report.columns if c.is_constant)
    dupe_rows = sum(d.duplicate_rows for d in report.duplicates)
    return {
        "columns_profiled": len(report.columns),
        "high_null_columns": high_null,
        "constant_columns": constant,
        "tables_with_duplicates": len(report.duplicates),
        "duplicate_rows": dupe_rows,
        "degraded": len(report.errors),
    }
