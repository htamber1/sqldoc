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
import time
from dataclasses import dataclass, field

from sqldoc.dbutil import cell


# SQLSTATE classes that mean the *connection* is broken (vs. a per-column data
# error like an un-groupable large-object column). 08xxx = connection
# exceptions; HYTxx = timeouts that on SQL Server usually mean the TCP link is
# already gone. When we see one of these mid-run we reconnect and retry the
# column rather than skipping it — and every column after it — on a dead cursor.
_CONN_LOST_SQLSTATES = frozenset({
    "08S01", "08S02", "08001", "08003", "08004", "08007", "HYT00", "HYT01",
})


def _is_connection_lost(exc) -> bool:
    """True if `exc` looks like a dropped/broken DB connection rather than a
    column-level data error.

    pyodbc raises with args like ('08S01', '[08S01]...Communication link
    failure...'); other drivers phrase it differently, so we check both the
    SQLSTATE (first arg) and the message text.
    """
    args = getattr(exc, "args", ()) or ()
    if args:
        state = str(args[0]).strip().strip("[]")[:5].upper()
        if state in _CONN_LOST_SQLSTATES:
            return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "communication link failure",
        "connection was aborted",
        "connection is closed",
        "connection reset",
        "server has gone away",
        "10053", "10054",              # WSAECONNABORTED / WSAECONNRESET
    ))


def _reconnect(adapter, old_conn, attempts=3, base_delay=1.0):
    """Discard a dead connection and open a fresh (connection, cursor), retrying
    a few times with exponential backoff. Raises the last error if every attempt
    fails (caller treats that as an unrecoverable outage)."""
    try:
        old_conn.close()
    except Exception:
        pass
    last = None
    for attempt in range(attempts):
        try:
            conn = adapter.connect()
            return conn, adapter.cursor(conn)
        except Exception as e:          # transient — back off and retry
            last = e
            time.sleep(base_delay * (2 ** attempt))
    raise last


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
    # Approximate distinct-count support, used ONLY above the heavy-stats row
    # threshold to avoid an exact COUNT(DISTINCT) sort/hash over tens of millions
    # of rows. `approx_distinct_sql` is a `{col}` template; `approx_probe_sql`
    # returns a column `ok` > 0 when the approximation is actually usable on this
    # server (SQL Server 2019+; the Postgres `hll` extension). None on both means
    # the dialect has no approximation, so oversized columns are skipped instead.
    approx_distinct_sql: str = None
    approx_probe_sql: str = None

    def quote(self, name: str) -> str:
        return self.q_open + (name or "").replace(self.q_close, self.q_esc_to) + self.q_close

    def approx_distinct(self, quoted_col: str):
        """Approximate distinct-count SQL for an already-quoted column, or None
        when this dialect/profile has no approximation configured."""
        return self.approx_distinct_sql.format(col=quoted_col) if self.approx_distinct_sql else None

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
    approx_distinct_sql="APPROX_COUNT_DISTINCT({col})",
    approx_probe_sql=("SELECT CASE WHEN CAST(SERVERPROPERTY('ProductMajorVersion') AS int) "
                      ">= 15 THEN 1 ELSE 0 END AS ok"),
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
    # Best-effort via the `hll` extension when installed; otherwise the probe
    # returns 0 and oversized columns are skipped. (Untested here — no Postgres
    # instance available — but the skip fallback is always safe.)
    approx_distinct_sql="hll_cardinality(hll_add_agg(hll_hash_any({col})))",
    approx_probe_sql="SELECT COUNT(*) AS ok FROM pg_extension WHERE extname = 'hll'",
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


def _detect_approx_distinct(cursor, profile) -> bool:
    """Probe whether this server supports an approximate distinct count for
    large-table profiling (SQL Server 2019+ APPROX_COUNT_DISTINCT; the Postgres
    `hll` extension). Returns False for dialects/servers without it, so the caller
    skips the distinct count on oversized tables instead of running an exact
    COUNT(DISTINCT). Any probe error is treated as 'not available'."""
    probe = getattr(profile, "approx_probe_sql", None)
    if not probe or not getattr(profile, "approx_distinct_sql", None):
        return False
    try:
        cursor.execute(probe)
        r = _first(cursor)
        return r is not None and int(cell(r, "ok") or 0) > 0
    except Exception:
        return False


def analyze_column_quality(cursor, schema, table, column, data_type,
                           top_values=5, profile=_SQLSERVER,
                           row_count=None, heavy_max_rows=None,
                           approx_distinct_ok=False) -> ColumnQuality:
    tbl = profile.qualify(schema, table)
    col = profile.quote(column)
    is_string, is_comparable, groupable = profile.classify(data_type)

    # "Heavy" = a table so large that an exact COUNT(DISTINCT), MIN/MAX, and the
    # top-values GROUP BY would each scan/sort tens of millions of rows per
    # column. Above the threshold we approximate the distinct count where the
    # server supports it (else skip it) and skip MIN/MAX + top-values entirely.
    # A falsy heavy_max_rows (0/None) disables the guard — exact stats as before.
    heavy = bool(heavy_max_rows) and row_count is not None and row_count > heavy_max_rows

    if not groupable:
        distinct_expr = "-1"
    elif not heavy:
        distinct_expr = f"COUNT(DISTINCT {col})"
    else:
        approx_expr = profile.approx_distinct(col) if approx_distinct_ok else None
        distinct_expr = approx_expr if approx_expr is not None else "-1"

    blank_expr = (f"SUM(CASE WHEN TRIM({col}) = '' THEN 1 ELSE 0 END)"
                  if is_string else "0")
    # MIN/MAX only for order-comparable types, and skipped on heavy tables.
    # Stringify in Python (no CAST, so the SQL stays dialect-neutral).
    do_minmax = is_comparable and not heavy
    min_expr = f"MIN({col})" if do_minmax else "NULL"
    max_expr = f"MAX({col})" if do_minmax else "NULL"

    cursor.execute(
        f"SELECT COUNT(*) AS total, COUNT({col}) AS non_null, "  # nosec B608 - reviewed: only int-cast counts and dialect-quoted catalog identifiers interpolated, never raw user input (see SECURITY.md)
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
    if groupable and top_values and non_null > 0 and not heavy:
        if profile.use_limit:
            top_sql = (f"SELECT {col} AS val, COUNT(*) AS freq FROM {tbl} "  # nosec B608 - reviewed: only int-cast counts and dialect-quoted catalog identifiers interpolated, never raw user input (see SECURITY.md)
                       f"WHERE {col} IS NOT NULL GROUP BY {col} "
                       f"ORDER BY COUNT(*) DESC LIMIT {int(top_values)}")
        else:
            top_sql = (f"SELECT TOP ({int(top_values)}) {col} AS val, COUNT(*) AS freq "  # nosec B608 - reviewed: only int-cast counts and dialect-quoted catalog identifiers interpolated, never raw user input (see SECURITY.md)
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


# Sentinel returned by detect_duplicates when a table exceeds the row-count
# threshold: full-row duplicate detection (GROUP BY every column) is O(rows) and
# can run for many minutes / effectively hang on very large tables, so we skip it
# and let the caller record a clear note instead of blocking the whole run.
_DUP_SKIPPED_TOO_LARGE = object()


def detect_duplicates(cursor, schema, table, columns, profile=_SQLSERVER,
                      row_count=None, max_rows=None):
    """Full-row duplicate detection: group by every groupable, non-computed
    column and count combinations that appear more than once.

    On tables whose `row_count` exceeds `max_rows` (when both are provided), the
    check is skipped up front — before any query runs — and
    `_DUP_SKIPPED_TOO_LARGE` is returned so the caller can note it. This keeps
    `quality` responsive on large databases: the GROUP-BY-all-columns scan is the
    heaviest query the profiler issues, and on tens-of-millions-of-rows tables it
    can run for many minutes. A falsy `max_rows` (0/None) disables the guard."""
    if max_rows and row_count is not None and row_count > max_rows:
        return _DUP_SKIPPED_TOO_LARGE
    groupable = [c for c in columns
                 if not c.is_computed and profile.classify(c.data_type)[2]]
    if not groupable:
        return None
    tbl = profile.qualify(schema, table)
    collist = ", ".join(profile.quote(c.name) for c in groupable)
    cursor.execute(
        f"SELECT COALESCE(SUM(cnt), 0) AS dup_rows, COUNT(*) AS dup_groups FROM ("  # nosec B608 - reviewed: only int-cast counts and dialect-quoted catalog identifiers interpolated, never raw user input (see SECURITY.md)
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
                    detect_dupes=True, dup_max_rows=5_000_000,
                    heavy_max_rows=5_000_000, progress=None) -> QualityReport:
    report = QualityReport(database="")
    allow = set(schemas) if schemas else None
    profile = profile_for(getattr(adapter, "dialect", "sqlserver"))
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    # Detect once whether this server can approximate distinct counts, so heavy
    # (oversized) tables get an APPROX_COUNT_DISTINCT instead of a skipped one.
    approx_ok = _detect_approx_distinct(cursor, profile)
    # Set once the connection is lost AND cannot be re-established: from there
    # on every query would just fail on the dead handle, so we stop and record
    # one honest error instead of thousands of cascading "link failure" skips.
    aborted = False

    def _run(work):
        """Run work(cursor); on a *connection* loss, reconnect once and retry.
        Returns (result, error_message_or_None). Column-level data errors are
        returned as messages (skip this column, keep going); an unrecoverable
        connection loss sets `aborted`."""
        nonlocal conn, cursor, aborted
        try:
            return work(cursor), None
        except Exception as e:
            if not _is_connection_lost(e):
                return None, f"{type(e).__name__}: {e}"
            try:
                conn, cursor = _reconnect(adapter, conn)
            except Exception as re:
                aborted = True
                return None, (f"database connection lost and could not be "
                              f"re-established: {type(re).__name__}: {re}")
            try:
                return work(cursor), None
            except Exception as e2:
                return None, f"{type(e2).__name__}: {e2}"

    try:
        targets = [t for t in tables if allow is None or t.schema in allow]
        for i, t in enumerate(targets):
            if progress:
                progress(i + 1, len(targets), t)
            for col in t.columns:
                if col.is_computed:
                    continue
                cq, err = _run(lambda cur: analyze_column_quality(
                    cur, t.schema, t.name, col.name, col.data_type,
                    top_values=top_values, profile=profile,
                    row_count=t.row_count, heavy_max_rows=heavy_max_rows,
                    approx_distinct_ok=approx_ok))
                if err is not None:
                    report.errors.append((f"{t.schema}.{t.name}.{col.name}", err))
                elif cq is not None:
                    report.columns.append(cq)
                if aborted:
                    break
            heavy = (bool(heavy_max_rows) and t.row_count is not None
                     and t.row_count > heavy_max_rows)
            if not aborted and heavy:
                if approx_ok:
                    report.errors.append((
                        f"{t.schema}.{t.name} (heavy stats)",
                        f"table too large ({t.row_count:,} rows > {heavy_max_rows:,} "
                        f"row threshold): distinct counts are APPROXIMATE "
                        f"(APPROX_COUNT_DISTINCT); MIN/MAX and top-values skipped"))
                else:
                    report.errors.append((
                        f"{t.schema}.{t.name} (heavy stats)",
                        f"n/a (table too large): {t.row_count:,} rows > {heavy_max_rows:,} "
                        f"row threshold; distinct count, MIN/MAX and top-values skipped "
                        f"(no approximate distinct available on this server)"))
            if not aborted and detect_dupes:
                dg, err = _run(lambda cur: detect_duplicates(
                    cur, t.schema, t.name, t.columns, profile=profile,
                    row_count=t.row_count, max_rows=dup_max_rows))
                if dg is _DUP_SKIPPED_TOO_LARGE:
                    report.errors.append((
                        f"{t.schema}.{t.name} (duplicates)",
                        f"skipped — table too large for duplicate check "
                        f"({(t.row_count or 0):,} rows > {dup_max_rows:,} row "
                        f"threshold; raise --duplicate-max-rows or use --no-duplicates)"))
                elif err is not None:
                    report.errors.append((f"{t.schema}.{t.name} (duplicates)", err))
                elif dg is not None:
                    report.duplicates.append(dg)
            if aborted:
                remaining = len(targets) - (i + 1)
                report.errors.append((
                    "connection",
                    f"database connection lost during {t.schema}.{t.name} and "
                    f"could not be re-established; stopped after {i + 1} of "
                    f"{len(targets)} table(s), {remaining} not profiled"))
                break
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
