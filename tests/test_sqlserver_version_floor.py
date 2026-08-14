"""Version-floor regression tests for sqldoc's SQL Server code paths.

Background: v3.0.4 shipped a hard, undocumented SQL Server 2017+ floor. Two
built-ins (STRING_AGG, TRIM) were used unguarded in code paths that run against
every SQL Server, so `sqldoc` failed outright on 2016 and older. Neither was
CMS-specific; a bulk run merely made them visible by spanning mixed versions.

Three layers, cheapest first:

  1. `TestOfflineVersionFloor` -- no database. Walks the SQL string literals in
     the SQL Server code paths and fails on any built-in whose version floor is
     above SQLSERVER_MIN_MAJOR unless it is explicitly registered as guarded.
     This is the guard against recurrence and is the one that must run in CI.

  2. `TestFixShape` -- no database. Asserts the specific v3.0.4 fixes are still
     in place, so a well-meaning "simplify" back to TRIM/STRING_AGG trips.

  3. `TestAgainstLiveServer` -- opt-in integration. Compiles the real queries
     with SET NOEXEC ON, which binds and name-resolves without executing, and
     asserts the server accepts them. Point SQLDOC_TEST_DSN at an instance at
     or below the supported floor (a 2016 box is the interesting case).

     Empirically, SET PARSEONLY ON is also enough to raise Msg 195 for an
     unknown built-in, but NOEXEC additionally resolves catalog objects and
     columns, so it catches version-gated *columns* too. Neither executes the
     query, so both are safe to run against any instance.

Run:
    pytest test_sqlserver_version_floor.py                     # layers 1-2
    SQLDOC_SRC=/path/to/sqldoc pytest ...                      # audit a checkout
    SQLDOC_TEST_DSN="DRIVER={ODBC Driver 17 for SQL Server};SERVER=...;\
DATABASE=master;TrustServerCertificate=yes;Trusted_Connection=yes;" pytest ...
"""
import ast
import os
import re
import pytest

# The oldest SQL Server major version sqldoc *tests and supports*. 13 = 2016.
#
# This is a support/documentation statement and the threshold for the audit
# below. It is NOT a runtime cutoff: sqldoc does not check the server version
# and does not refuse to run on anything older. Below 2016 is untested, not
# blocked -- core documentation may well work on 2012/2014 because the catalog
# views involved are 2005-era. Changing this value changes what the audit
# demands a guard for; see NOTES.md for the full support position.
SQLSERVER_MIN_MAJOR = 13

# Built-ins that do not exist on every supported version, and the major version
# that introduced them. Anything with a floor above SQLSERVER_MIN_MAJOR needs a
# fallback or a runtime guard before it may appear in a SQL Server code path.
VERSION_GATED = {
    "STRING_AGG":            (14, "2017"),
    "TRIM":                  (14, "2017"),
    "CONCAT_WS":             (14, "2017"),
    "TRANSLATE":             (14, "2017"),
    "APPROX_COUNT_DISTINCT": (15, "2019"),
    "GREATEST":              (16, "2022"),
    "LEAST":                 (16, "2022"),
    "GENERATE_SERIES":       (16, "2022"),
    "DATE_BUCKET":           (16, "2022"),
    "STRING_SPLIT":          (13, "2016"),
    "JSON_VALUE":            (13, "2016"),
    "JSON_QUERY":            (13, "2016"),
    "OPENJSON":              (13, "2016"),
    "COMPRESS":              (13, "2016"),
    "DECOMPRESS":            (13, "2016"),
    "DATEDIFF_BIG":          (13, "2016"),
    "STRING_ESCAPE":         (13, "2016"),
    "SESSION_CONTEXT":       (13, "2016"),
}

# Modules whose SQL runs against SQL Server. Dialect-specific modules for other
# engines are deliberately excluded: TRIM in the Redshift adapter and GREATEST
# in the MySQL slow-query collector are correct for those engines.
SQLSERVER_MODULES = (
    "adapters/sqlserver.py",
    "quality.py",
    "cms.py",
    "cms_bulk.py",
    "cms_report.py",
)

# Usages that are allowed because a runtime guard keeps them off older servers.
# Key: (module, builtin). Value: how it is guarded -- reviewed, not waved through.
GUARDED = {
    ("quality.py", "APPROX_COUNT_DISTINCT"):
        "QualityProfile.approx_probe_sql probes "
        "SERVERPROPERTY('ProductMajorVersion') >= 15 and falls back to a skipped "
        "distinct count when the probe returns 0.",
}


def _package_root():
    root = os.environ.get("SQLDOC_SRC")
    if root:
        return root
    import sqldoc
    return os.path.dirname(sqldoc.__file__)


def _sql_literals(path):
    """Every string literal in a module, with f-strings reconstructed into a
    single string (placeholders collapsed to `{}`).

    Reconstruction matters: `f"...LTRIM(RTRIM({col}))..."` is stored in the AST
    as two separate Constant fragments split at the placeholder, so scanning raw
    fragments would both miss a call spanning that boundary and misreport one
    that does not. Using the AST rather than raw file text also means comments
    and docstrings that merely *mention* a built-in do not trip the scan.
    """
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    consumed, out = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    consumed.add(id(value))
                    parts.append(value.value)
                else:
                    parts.append("{}")
            out.append((getattr(node, "lineno", 0), "".join(parts)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in consumed or node.value in docstrings:
                continue
            out.append((getattr(node, "lineno", 0), node.value))
    return out


def _scan(path):
    """Return [(builtin, floor, label, lineno)] for gated built-ins in `path`."""
    hits = []
    for lineno, text in _sql_literals(path):
        for name, (floor, label) in VERSION_GATED.items():
            # \b keeps LTRIM(/RTRIM( from matching TRIM(, and requires a call.
            if re.search(r"\b%s\s*\(" % name, text, re.IGNORECASE):
                hits.append((name, floor, label, lineno))
    return hits


class TestOfflineVersionFloor:
    """No database required. This is the layer that must run in CI."""

    def test_no_unguarded_version_gated_builtins(self):
        root = _package_root()
        violations = []
        for rel in SQLSERVER_MODULES:
            path = os.path.join(root, *rel.split("/"))
            if not os.path.exists(path):
                continue
            for name, floor, label, lineno in _scan(path):
                if floor <= SQLSERVER_MIN_MAJOR:
                    continue
                key = (rel, name)
                if key in GUARDED:
                    continue
                violations.append(
                    "%s:%d uses %s (SQL Server %s / major %d) but sqldoc "
                    "supports major %d and up, with no fallback or runtime guard."
                    % (rel, lineno, name, label, floor, SQLSERVER_MIN_MAJOR))
        assert not violations, (
            "Unguarded version-gated T-SQL in SQL Server code paths:\n  "
            + "\n  ".join(violations)
            + "\n\nFix with a portable equivalent (preferred) or a runtime probe "
              "like QualityProfile.approx_probe_sql. If the floor is being raised "
              "on purpose, change SQLSERVER_MIN_MAJOR and say so in NOTES.md.")

    def test_guard_registry_entries_are_real(self):
        """A stale GUARDED entry would silently permit a future regression."""
        root = _package_root()
        for (rel, name), why in GUARDED.items():
            path = os.path.join(root, *rel.split("/"))
            if not os.path.exists(path):
                continue
            found = [h for h in _scan(path) if h[0] == name]
            assert found, (
                "GUARDED lists %s in %s but it no longer appears there. Drop the "
                "entry so the allowlist keeps meaning something. (Recorded "
                "rationale: %s)" % (name, rel, why))

    def test_approx_count_distinct_probe_still_checks_version(self):
        """The one guarded usage: assert the guard itself is intact."""
        from sqldoc.quality import _SQLSERVER
        probe = (_SQLSERVER.approx_probe_sql or "").upper()
        assert "PRODUCTMAJORVERSION" in probe, (
            "The APPROX_COUNT_DISTINCT guard no longer probes the server "
            "version; it would now be attempted on pre-2019 servers.")
        assert ">= 15" in probe.replace(" ", " "), (
            "APPROX_COUNT_DISTINCT requires major 15 (2019); the probe no "
            "longer checks that threshold.")


class TestFixShape:
    """Pin the specific v3.0.4 fixes so a later 'cleanup' cannot undo them."""

    def test_trigger_query_uses_portable_aggregation(self):
        root = _package_root()
        path = os.path.join(root, "adapters", "sqlserver.py")
        trigger_sql = [t for _, t in _sql_literals(path)
                       if "sys.trigger_events" in t and "SELECT" in t.upper()]
        assert trigger_sql, "Could not locate the trigger query in the adapter."
        sql = trigger_sql[0]
        assert not re.search(r"\bSTRING_AGG\s*\(", sql, re.IGNORECASE), (
            "The trigger query uses STRING_AGG again. It requires SQL Server "
            "2017+, and because the query is not isolated a parse failure here "
            "aborts all of extract_metadata -- yielding no metadata at all on "
            "an older instance, not merely triggers without events.")
        assert "FOR XML PATH" in sql.upper(), (
            "The trigger query no longer uses the portable "
            "STUFF(... FOR XML PATH('')) aggregation.")

    def test_blank_count_uses_portable_trim(self):
        root = _package_root()
        path = os.path.join(root, "quality.py")
        blanks = [t for _, t in _sql_literals(path) if "THEN 1 ELSE 0 END" in t]
        assert blanks, "Could not locate the blank-count expression in quality.py."
        joined = " ".join(blanks)
        assert not re.search(r"\bTRIM\s*\(", joined, re.IGNORECASE), (
            "The blank-count expression uses TRIM() again. TRIM is SQL Server "
            "2017+ and this expression is built for every string column on "
            "every dialect. Use LTRIM(RTRIM(...)).")
        assert "LTRIM(RTRIM(" in joined.replace(" ", ""), (
            "The blank-count expression no longer uses LTRIM(RTRIM(...)).")


# --- opt-in integration ----------------------------------------------------

DSN = os.environ.get("SQLDOC_TEST_DSN")

# Compiled, never executed. Each mirrors a real sqldoc query shape.
CORPUS = {
    "trigger_events_aggregation": """
        SELECT s.name AS schema_name, t.name AS table_name, tr.name AS trigger_name,
               tr.is_instead_of_trigger, tr.is_disabled, m.definition,
               STUFF((SELECT ',' + te.type_desc
                      FROM sys.trigger_events te
                      WHERE te.object_id = tr.object_id
                      ORDER BY te.type_desc
                      FOR XML PATH('')), 1, 1, '') AS events
        FROM sys.triggers tr
        INNER JOIN sys.tables t ON tr.parent_id = t.object_id
        INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
        LEFT JOIN sys.sql_modules m ON tr.object_id = m.object_id
        WHERE tr.parent_class = 1 AND tr.is_ms_shipped = 0
        ORDER BY s.name, t.name, tr.name
    """,
    "blank_count_expression": """
        SELECT COUNT(*) AS total, COUNT(name) AS non_null,
               COUNT(DISTINCT name) AS distinct_count,
               SUM(CASE WHEN LTRIM(RTRIM(name)) = '' THEN 1 ELSE 0 END) AS blank_count,
               MIN(name) AS min_val, MAX(name) AS max_val
        FROM sys.objects
    """,
    "version_probe": """
        SELECT CASE WHEN CAST(SERVERPROPERTY('ProductMajorVersion') AS int) >= 15
               THEN 1 ELSE 0 END AS ok
    """,
}


def _connect():
    import pyodbc
    return pyodbc.connect(DSN, timeout=10)


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="set SQLDOC_TEST_DSN to run integration tests")
class TestAgainstLiveServer:

    @pytest.fixture(scope="class")
    def major(self):
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int)")
            return int(cur.fetchone()[0])

    def test_target_version_is_reported(self, major):
        """Pointing this suite at a pre-2016 instance is explicitly allowed --
        older versions are best-effort, not blocked -- so being below the floor
        is reported, never failed."""
        assert major > 0, "Could not read ProductMajorVersion from the target."
        if major < SQLSERVER_MIN_MAJOR:
            pytest.skip(
                "Target is major %d, below the tested floor of %d. Results here "
                "are a best-effort data point for whether to extend the "
                "supported range, not a pass/fail signal."
                % (major, SQLSERVER_MIN_MAJOR))

    @pytest.mark.parametrize("name", sorted(CORPUS))
    def test_query_compiles(self, name, major):
        """SET NOEXEC ON binds and name-resolves without running the query, so
        an unknown built-in raises Msg 195 here exactly as it would in anger."""
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SET NOEXEC ON")
            try:
                cur.execute(CORPUS[name])
            except Exception as exc:  # pragma: no cover - the failure is the point
                if major < SQLSERVER_MIN_MAJOR:
                    # Below the supported floor this is information, not a
                    # regression: it tells you what extending support would cost.
                    pytest.skip(
                        "%s does not compile on major %d, which is below the "
                        "tested floor of %d. Below-floor behaviour is "
                        "best-effort and unclaimed. Detail: %s"
                        % (name, major, SQLSERVER_MIN_MAJOR, exc))
                pytest.fail(
                    "%s failed to compile on SQL Server major %d: %s"
                    % (name, major, exc))

    def test_string_agg_really_is_unavailable_below_2017(self, major):
        """Proves the 2016 path is genuinely being exercised rather than the
        suite quietly passing against a modern server."""
        if major >= 14:
            pytest.skip("target is major %d; STRING_AGG exists here" % major)
        import pyodbc
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SET NOEXEC ON")
            with pytest.raises(pyodbc.Error) as err:
                cur.execute("SELECT STRING_AGG(name, ',') FROM sys.objects")
            assert "STRING_AGG" in str(err.value)

    def test_portable_aggregation_matches_string_agg(self, major):
        """Equivalence check, where both forms exist (2017+)."""
        if major < 14:
            pytest.skip("STRING_AGG needs major 14+; nothing to compare against")
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT TOP 25 o.object_id,
                  STRING_AGG(c.name, ',') AS via_string_agg,
                  STUFF((SELECT ',' + c2.name FROM sys.columns c2
                         WHERE c2.object_id = o.object_id ORDER BY c2.name
                         FOR XML PATH('')), 1, 1, '') AS via_xml_path
                FROM sys.objects o JOIN sys.columns c ON c.object_id = o.object_id
                GROUP BY o.object_id ORDER BY o.object_id
            """)
            rows = cur.fetchall()
        assert rows, "No rows to compare; pick a database with user objects."
        for r in rows:
            # STRING_AGG without WITHIN GROUP has unspecified order, so compare
            # as sets. The replacement adds ORDER BY and is deterministic.
            assert sorted((r.via_string_agg or "").split(",")) == \
                   sorted((r.via_xml_path or "").split(",")), (
                "Aggregation mismatch for object_id %s" % r.object_id)
