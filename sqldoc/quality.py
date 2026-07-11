"""Data-quality analysis: null rates, per-column distribution, and duplicate
records — across SQL Server, PostgreSQL, MySQL, and SQLite.

Unlike the documentation/scan paths, this reads **actual table data** — but only
in *aggregate* (COUNT / COUNT DISTINCT / MIN / MAX / GROUP BY). The one place raw
values surface is each column's most-frequent values (for the distribution
view); those are truncated and stay local — nothing is sent to any AI or
network. Every per-column query is isolated so one failure (e.g. an un-groupable
large-object column) records an error and the rest proceed.

The SQL is mostly ANSI; the per-dialect differences (identifier quoting, `TOP`
vs `LIMIT`, and which declared types can be grouped / compared) are captured in
a small `QualityProfile` looked up by the adapter's dialect.
"""
from dataclasses import dataclass, field

from sqldoc.dbutil import cell


@dataclass
class QualityProfile:
    """Per-dialect SQL fragments + type classification for profiling."""
    dialect: str
    q_open: str
    q_close: str
    q_esc_to: str            # what a literal quote char becomes inside an identifier
    use_limit: bool          # True -> `... LIMIT n`; False -> `SELECT TOP (n) ...`
    string_types: frozenset
    comparable_types: frozenset
    ungroupable_types: frozenset

    def quote(self, name: str) -> str:
        return self.q_open + (name or "").replace(self.q_close, self.q_esc_to) + self.q_close

    def qualify(self, schema: str, table: str) -> str:
        return f"{self.quote(schema)}.{self.quote(table)}"

    def _base_type(self, data_type: str) -> str:
        # Strip any length/precision: "nvarchar(70)" -> "nvarchar".
        return (data_type or "").lower().split("(")[0].strip()

    def classify(self, data_type: str):
        """Return (is_string, is_comparable, groupable) for a declared type."""
        dt = self._base_type(data_type)
        return (dt in self.string_types,
                dt in self.comparable_types,
                dt not in self.ungroupable_types)


_SQLSERVER = QualityProfile(
    dialect="sqlserver", q_open="[", q_close="]", q_esc_to="]]", use_limit=False,
    string_types=frozenset({"char", "varchar", "nchar", "nvarchar", "text", "ntext"}),
    comparable_types=frozenset({"int", "bigint", "smallint", "tinyint", "decimal",
                                "numeric", "money", "smallmoney", "float", "real",
                                "date", "datetime", "datetime2", "smalldatetime",
                                "datetimeoffset"}),
    ungroupable_types=frozenset({"text", "ntext", "image", "xml", "geography",
                                 "geometry", "hierarchyid", "sql_variant"}),
)

_POSTGRES = QualityProfile(
    dialect="postgres", q_open='"', q_close='"', q_esc_to='""', use_limit=True,
    string_types=frozenset({"character varying", "varchar", "character", "char",
                            "text", "name", "citext"}),
    comparable_types=frozenset({"smallint", "integer", "bigint", "decimal", "numeric",
                                "real", "double precision", "money", "date",
                                "timestamp without time zone", "timestamp with time zone",
                                "time without time zone", "time with time zone"}),
    ungroupable_types=frozenset({"json", "jsonb", "xml", "bytea", "tsvector", "tsquery",
                                 "array", "user-defined", "point", "polygon", "hstore"}),
)

_MYSQL = QualityProfile(
    dialect="mysql", q_open="`", q_close="`", q_esc_to="``", use_limit=True,
    string_types=frozenset({"char", "varchar", "tinytext", "text", "mediumtext",
                            "longtext", "enum", "set"}),
    comparable_types=frozenset({"tinyint", "smallint", "mediumint", "int", "integer",
                                "bigint", "decimal", "numeric", "float", "double",
                                "date", "datetime", "timestamp", "time", "year"}),
    ungroupable_types=frozenset({"blob", "tinyblob", "mediumblob", "longblob", "json",
                                 "geometry", "point", "linestring", "polygon"}),
)

_SQLITE = QualityProfile(
    dialect="sqlite", q_open='"', q_close='"', q_esc_to='""', use_limit=True,
    string_types=frozenset({"text", "varchar", "nvarchar", "char", "nchar", "clob",
                            "character"}),
    comparable_types=frozenset({"integer", "int", "bigint", "smallint", "tinyint",
                                "real", "numeric", "decimal", "double", "float",
                                "date", "datetime"}),
    ungroupable_types=frozenset({"blob"}),
)

_PROFILES = {
    "sqlserver": _SQLSERVER, "azuresql": _SQLSERVER,
    "postgres": _POSTGRES, "mysql": _MYSQL, "sqlite": _SQLITE,
}


def profile_for(dialect: str) -> QualityProfile:
    return _PROFILES.get(dialect, _SQLSERVER)


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


def _first(cursor):
    rows = cursor.fetchall()
    return rows[0] if rows else None


def analyze_column_quality(cursor, schema, table, column, data_type,
                           top_values=5, profile=_SQLSERVER) -> ColumnQuality:
    tbl = profile.qualify(schema, table)
    col = profile.quote(column)
    is_string, is_comparable, groupable = profile.classify(data_type)

    distinct_expr = f"COUNT(DISTINCT {col})" if groupable else "-1"
    blank_expr = (f"SUM(CASE WHEN TRIM({col}) = '' THEN 1 ELSE 0 END)"
                  if is_string else "0")
    # MIN/MAX only for order-comparable types; stringify in Python (no CAST, so
    # the SQL stays dialect-neutral).
    min_expr = f"MIN({col})" if is_comparable else "NULL"
    max_expr = f"MAX({col})" if is_comparable else "NULL"

    cursor.execute(
        f"SELECT COUNT(*) AS total, COUNT({col}) AS non_null, "
        f"{distinct_expr} AS distinct_count, {blank_expr} AS blank_count, "
        f"{min_expr} AS min_val, {max_expr} AS max_val FROM {tbl}"
    )
    r = _first(cursor)
    if r is None:
        return None
    total = int(cell(r, "total") or 0)
    non_null = int(cell(r, "non_null") or 0)
    null_count = total - non_null

    top = []
    if groupable and top_values and non_null > 0:
        if profile.use_limit:
            top_sql = (f"SELECT {col} AS val, COUNT(*) AS freq FROM {tbl} "
                       f"WHERE {col} IS NOT NULL GROUP BY {col} "
                       f"ORDER BY COUNT(*) DESC LIMIT {int(top_values)}")
        else:
            top_sql = (f"SELECT TOP ({int(top_values)}) {col} AS val, COUNT(*) AS freq "
                       f"FROM {tbl} WHERE {col} IS NOT NULL GROUP BY {col} "
                       f"ORDER BY COUNT(*) DESC")
        cursor.execute(top_sql)
        for row in cursor.fetchall():
            v = cell(row, "val")
            top.append({"value": ("" if v is None else str(v))[:80],
                        "count": int(cell(row, "freq") or 0)})

    dc = cell(r, "distinct_count")
    mn = cell(r, "min_val")
    mx = cell(r, "max_val")
    return ColumnQuality(
        schema=schema, table=table, column=column, data_type=data_type,
        total_rows=total, null_count=null_count,
        null_rate=round(null_count / total, 4) if total else 0.0,
        distinct_count=int(dc) if dc is not None else -1,
        blank_count=int(cell(r, "blank_count") or 0),
        min_value="" if mn is None else str(mn),
        max_value="" if mx is None else str(mx),
        top_values=top,
    )


def detect_duplicates(cursor, schema, table, columns, profile=_SQLSERVER) -> DuplicateGroup:
    """Full-row duplicate detection: group by every groupable, non-computed
    column and count combinations that appear more than once."""
    groupable = [c for c in columns
                 if not c.is_computed and profile.classify(c.data_type)[2]]
    if not groupable:
        return None
    tbl = profile.qualify(schema, table)
    collist = ", ".join(profile.quote(c.name) for c in groupable)
    cursor.execute(
        f"SELECT COALESCE(SUM(cnt), 0) AS dup_rows, COUNT(*) AS dup_groups FROM ("
        f"SELECT COUNT(*) AS cnt FROM {tbl} GROUP BY {collist} HAVING COUNT(*) > 1) g"
    )
    r = _first(cursor)
    if r is None:
        return None
    groups = int(cell(r, "dup_groups") or 0)
    total_in_groups = int(cell(r, "dup_rows") or 0)
    if groups == 0:
        return None
    return DuplicateGroup(
        schema=schema, table=table, columns_considered=[c.name for c in groupable],
        duplicate_groups=groups, duplicate_rows=total_in_groups - groups,
    )


def collect_quality(adapter, tables, top_values=5, schemas=None,
                    detect_dupes=True, progress=None) -> QualityReport:
    report = QualityReport(database="")
    allow = set(schemas) if schemas else None
    profile = profile_for(getattr(adapter, "dialect", "sqlserver"))
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
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
                                                col.data_type, top_values=top_values,
                                                profile=profile)
                    if cq is not None:
                        report.columns.append(cq)
                except Exception as e:
                    report.errors.append((f"{t.schema}.{t.name}.{col.name}",
                                          f"{type(e).__name__}: {e}"))
            if detect_dupes:
                try:
                    dg = detect_duplicates(cursor, t.schema, t.name, t.columns, profile=profile)
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
