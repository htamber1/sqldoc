"""Schema intelligence: naming conventions, orphaned FKs, impact analysis, and
migration-script generation.

All of this works on the already-extracted metadata model (tables/views/
procedures) plus schema snapshots — it issues no extra queries and reads no row
data. The four analyses:

* **Naming conventions** — infer the dominant identifier style (Pascal / snake /
  camel / UPPER) for tables and columns and flag the outliers, plus a
  primary-key naming check.
* **Orphaned FKs** — columns named like a foreign key (``CustomerID``) that a
  table exists for, but which carry no actual FK constraint: an implied,
  unenforced relationship.
* **Impact analysis** — for each table, what depends on it (FKs pointing at it,
  and views/procedures/triggers whose SQL references it): "what breaks if you
  drop this".
* **Migration generation** — a best-effort DDL script from a schema diff
  (baseline snapshot → current), for review before applying.
"""
import re
from dataclasses import dataclass, field

from sqldoc.dbutil import cell
from sqldoc.snapshot import build_snapshot, diff_snapshots


@dataclass
class NamingIssue:
    kind: str            # table-case / column-case / pk-naming / pluralization
    object: str          # the identifier in question
    detail: str
    suggestion: str


@dataclass
class OrphanFK:
    schema: str
    table: str
    column: str
    implied_table: str
    detail: str


@dataclass
class TableImpact:
    schema: str
    table: str
    fk_dependents: list = field(default_factory=list)      # "schema.table.column" FKs -> this table
    view_dependents: list = field(default_factory=list)    # "schema.view"
    proc_dependents: list = field(default_factory=list)    # "schema.proc"
    trigger_dependents: list = field(default_factory=list)  # "schema.table.trigger"

    @property
    def total(self) -> int:
        return (len(self.fk_dependents) + len(self.view_dependents)
                + len(self.proc_dependents) + len(self.trigger_dependents))


@dataclass
class LinkedServerLogin:
    local_login: str
    remote_login: str
    uses_self: bool = False


@dataclass
class LinkedServer:
    name: str
    product: str = ""
    provider: str = ""
    data_source: str = ""
    catalog: str = ""
    is_rpc_out: bool = False
    is_data_access: bool = False
    is_remote_login: bool = False
    logins: list = field(default_factory=list)      # LinkedServerLogin
    reachable: bool = None                           # None = not tested
    connectivity_message: str = ""
    remote_version: str = ""
    remote_edition: str = ""


@dataclass
class LinkedServerReport:
    local_server: str = ""
    linked_servers: list = field(default_factory=list)   # LinkedServer
    traversed: bool = False
    errors: list = field(default_factory=list)


@dataclass
class IntelReport:
    database: str
    naming_issues: list = field(default_factory=list)
    orphan_fks: list = field(default_factory=list)
    impacts: list = field(default_factory=list)
    migration_sql: str = ""       # populated only when a baseline snapshot is supplied
    linked_servers: object = None  # LinkedServerReport, when --linked-servers is used


# --- Naming conventions ----------------------------------------------------

def classify_case(name: str) -> str:
    if "_" in name:
        return "snake"
    if name.isupper():
        return "UPPER"
    letters = [c for c in name if c.isalpha()]
    if letters and name[0].isupper():
        return "Pascal"
    if letters and name[0].islower():
        return "camel"
    return "other"


def _dominant(values):
    """Return (style, share) for the most common style, ignoring 'other'."""
    counts = {}
    for v in values:
        if v == "other":
            continue
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None, 0.0
    total = sum(counts.values())
    style = max(counts, key=counts.get)
    return style, counts[style] / total


def analyze_naming(tables) -> list:
    issues = []

    # Table-name casing vs the dominant style (need a clear-ish majority).
    table_styles = {t.name: classify_case(t.name) for t in tables}
    dom, share = _dominant(table_styles.values())
    if dom and share >= 0.6:
        for name, style in sorted(table_styles.items()):
            if style not in (dom, "other"):
                issues.append(NamingIssue(
                    "table-case", name,
                    f"{style} name in a mostly-{dom} schema",
                    f"Rename to {dom} casing for consistency."))

    # Column-name casing across the whole schema.
    col_styles = {}
    for t in tables:
        for c in t.columns:
            col_styles.setdefault(f"{t.name}.{c.name}", classify_case(c.name))
    dom_c, share_c = _dominant(col_styles.values())
    if dom_c and share_c >= 0.6:
        for name, style in sorted(col_styles.items()):
            if style not in (dom_c, "other"):
                issues.append(NamingIssue(
                    "column-case", name,
                    f"{style} column in a mostly-{dom_c} schema",
                    f"Rename to {dom_c} casing for consistency."))

    # Primary-key naming: infer the dominant pattern for single-column PKs.
    def pk_pattern(table_name, col_name):
        low = col_name.lower()
        if low == "id":
            return "Id"
        if low == f"{table_name.lower()}id":
            return "TableId"
        return "other"

    pk_patterns = {}
    for t in tables:
        pks = [c for c in t.columns if c.is_primary_key]
        if len(pks) == 1:
            pk_patterns[f"{t.name}.{pks[0].name}"] = pk_pattern(t.name, pks[0].name)
    dom_pk, share_pk = _dominant(pk_patterns.values())
    if dom_pk and share_pk >= 0.6:
        for name, pat in sorted(pk_patterns.items()):
            if pat not in (dom_pk, "other"):
                issues.append(NamingIssue(
                    "pk-naming", name,
                    f"PK named unlike the dominant '{dom_pk}' pattern",
                    "Align single-column PK naming across tables."))

    return issues


# --- Orphaned foreign keys -------------------------------------------------

def _table_name_index(tables):
    """Lowercased table names plus a naive de-pluralized form, for matching an
    FK-shaped column prefix against an existing table."""
    idx = {}
    for t in tables:
        low = t.name.lower()
        idx[low] = t.name
        if low.endswith("s"):
            idx.setdefault(low[:-1], t.name)   # Customers -> customer
    return idx


_FK_COL = re.compile(r"^(?P<prefix>.+?)(?:id)$", re.IGNORECASE)


def detect_orphan_fks(tables) -> list:
    idx = _table_name_index(tables)
    orphans = []
    for t in tables:
        for c in t.columns:
            if c.is_foreign_key or c.is_primary_key:
                continue
            m = _FK_COL.match(c.name)
            if not m:
                continue
            prefix = m.group("prefix")
            if not prefix or prefix.lower() == t.name.lower():
                continue   # bare "Id" or self-reference-ish
            key = prefix.lower()
            target = idx.get(key) or idx.get(key + "s")
            if target and target.lower() != t.name.lower():
                orphans.append(OrphanFK(
                    schema=t.schema, table=t.name, column=c.name, implied_table=target,
                    detail=f"'{c.name}' looks like a reference to {target} but has no FK constraint."))
    return orphans


# --- Impact analysis -------------------------------------------------------

def _mentions(definition, table_name) -> bool:
    if not definition:
        return False
    return re.search(rf"\b{re.escape(table_name)}\b", definition, re.IGNORECASE) is not None


def analyze_impact(tables, views=None, procedures=None) -> list:
    views = views or []
    procedures = procedures or []
    impacts = []
    for t in tables:
        imp = TableImpact(schema=t.schema, table=t.name)
        # FKs from other tables pointing at this one (references_table is a bare name).
        for other in tables:
            for c in other.columns:
                if c.is_foreign_key and c.references_table == t.name:
                    imp.fk_dependents.append(f"{other.schema}.{other.name}.{c.name}")
        # Views / procedures / triggers whose SQL mentions this table.
        for v in views:
            if _mentions(v.definition, t.name):
                imp.view_dependents.append(f"{v.schema}.{v.name}")
        for p in procedures:
            if _mentions(p.definition, t.name):
                imp.proc_dependents.append(f"{p.schema}.{p.name}")
        for other in tables:
            for tg in other.triggers:
                if other.name != t.name and _mentions(tg.definition, t.name):
                    imp.trigger_dependents.append(f"{other.schema}.{other.name}.{tg.name}")
        impacts.append(imp)
    # Most-depended-upon first.
    impacts.sort(key=lambda i: i.total, reverse=True)
    return impacts


# --- Migration generation --------------------------------------------------

def _qual(key: str) -> str:
    schema, _, name = key.partition(".")
    return f"[{schema}].[{name}]"


def _col_ddl(name, shape) -> str:
    dtype = shape.get("type", "sql_variant")
    null = "NULL" if shape.get("nullable", True) else "NOT NULL"
    default = f" DEFAULT {shape['default']}" if shape.get("default") else ""
    return f"[{name}] {dtype} {null}{default}"


def _create_table(key, tinfo) -> str:
    cols = tinfo.get("columns", {})
    lines = [_col_ddl(n, s) for n, s in cols.items()]
    pks = [n for n, s in cols.items() if s.get("pk")]
    if pks:
        lines.append("PRIMARY KEY (" + ", ".join(f"[{p}]" for p in pks) + ")")
    body = ",\n    ".join(lines)
    return f"CREATE TABLE {_qual(key)} (\n    {body}\n);"


def generate_migration(old_snapshot: dict, new_snapshot: dict) -> str:
    """Best-effort DDL to move a database from `old_snapshot` to `new_snapshot`.

    Types come from the snapshot (name only — no length/precision), so the
    script is a **review starting point**, not a drop-in migration.
    """
    diff = diff_snapshots(old_snapshot, new_snapshot)
    new_t = new_snapshot.get("tables", {})
    out = ["-- Migration generated by sqldoc (review before running).",
           "-- Types lack length/precision; adjust as needed.",
           f"-- Database: {new_snapshot.get('database', '')}", ""]

    if not diff["has_changes"]:
        out.append("-- No schema changes detected.")
        return "\n".join(out)

    for key in diff["tables_added"]:
        out.append(_create_table(key, new_t.get(key, {})))
        out.append("")
    for key in diff["tables_removed"]:
        out.append(f"DROP TABLE {_qual(key)};")
    if diff["tables_removed"]:
        out.append("")

    for mod in diff["tables_modified"]:
        q = _qual(mod["name"])
        cols = new_t.get(mod["name"], {}).get("columns", {})
        for col in mod["added"]:
            out.append(f"ALTER TABLE {q} ADD {_col_ddl(col, cols.get(col, {}))};")
        for col in mod["removed"]:
            out.append(f"ALTER TABLE {q} DROP COLUMN [{col}];")
        for ch in mod["changed"]:
            for fld, old_v, new_v in ch["deltas"]:
                if fld == "type":
                    shape = cols.get(ch["name"], {})
                    null = "NULL" if shape.get("nullable", True) else "NOT NULL"
                    out.append(f"ALTER TABLE {q} ALTER COLUMN [{ch['name']}] {new_v} {null};")
                else:
                    out.append(f"-- {q} column [{ch['name']}]: {fld} {old_v} -> {new_v} (manual review)")
        checks = new_t.get(mod["name"], {}).get("checks", {})
        for name in mod.get("checks_added", []):
            out.append(f"ALTER TABLE {q} ADD CONSTRAINT [{name}] CHECK {checks.get(name, '(/* expr */)')};")
        for name in mod.get("checks_removed", []):
            out.append(f"ALTER TABLE {q} DROP CONSTRAINT [{name}];")
        uniques = new_t.get(mod["name"], {}).get("uniques", {})
        for name in mod.get("uniques_added", []):
            cols_u = ", ".join(f"[{c}]" for c in uniques.get(name, []))
            out.append(f"ALTER TABLE {q} ADD CONSTRAINT [{name}] UNIQUE ({cols_u});")
        for name in mod.get("uniques_removed", []):
            out.append(f"ALTER TABLE {q} DROP CONSTRAINT [{name}];")
        out.append("")

    for key in diff["views_added"]:
        out.append(f"-- TODO: CREATE VIEW {_qual(key)} (definition not captured in snapshot).")
    for key in diff["views_removed"]:
        out.append(f"DROP VIEW {_qual(key)};")
    for key in diff["procedures_added"]:
        out.append(f"-- TODO: CREATE PROCEDURE {_qual(key)} (definition not captured in snapshot).")
    for key in diff["procedures_removed"]:
        out.append(f"DROP PROCEDURE {_qual(key)};")

    return "\n".join(out).rstrip() + "\n"


# --- Orchestration ---------------------------------------------------------

# --- Linked server network mapping (SQL Server) ----------------------------

def _s(v) -> str:
    return "" if v is None else str(v)


def discover_linked_servers(cursor) -> list:
    cursor.execute("""
        SELECT s.name,
               s.product,
               s.provider,
               s.data_source,
               s.catalog,
               s.is_rpc_out_enabled,
               s.is_data_access_enabled,
               s.is_remote_login_enabled
        FROM sys.servers s
        WHERE s.is_linked = 1
        ORDER BY s.name
    """)
    out = []
    for r in cursor.fetchall():
        out.append(LinkedServer(
            name=_s(cell(r, "name")),
            product=_s(cell(r, "product")),
            provider=_s(cell(r, "provider")),
            data_source=_s(cell(r, "data_source")),
            catalog=_s(cell(r, "catalog")),
            is_rpc_out=bool(cell(r, "is_rpc_out_enabled")),
            is_data_access=bool(cell(r, "is_data_access_enabled")),
            is_remote_login=bool(cell(r, "is_remote_login_enabled")),
        ))
    return out


def get_linked_logins(cursor) -> dict:
    cursor.execute("""
        SELECT s.name AS linked_server,
               CASE WHEN ll.local_principal_id = 0 THEN '(all logins)'
                    ELSE SUSER_NAME(ll.local_principal_id) END AS local_login,
               ll.remote_name,
               ll.uses_self_credential
        FROM sys.linked_logins ll
        INNER JOIN sys.servers s ON ll.server_id = s.server_id
        WHERE s.is_linked = 1
        ORDER BY s.name
    """)
    by_server = {}
    for r in cursor.fetchall():
        by_server.setdefault(_s(cell(r, "linked_server")), []).append(LinkedServerLogin(
            local_login=_s(cell(r, "local_login")),
            remote_login=_s(cell(r, "remote_name")),
            uses_self=bool(cell(r, "uses_self_credential")),
        ))
    return by_server


def probe_connectivity(cursor, name):
    """Ping a linked server via sp_testlinkedserver, which raises if it cannot
    connect. Returns (reachable, message)."""
    try:
        cursor.execute("EXEC sys.sp_testlinkedserver ?", name)
        return True, "OK"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def probe_linked_server(cursor, name):
    """Run a lightweight remote query (via OPENQUERY) to fetch the linked
    server's version + edition. Returns (version, edition) or None."""
    safe = name.replace("]", "]]")
    try:
        cursor.execute(
            "SELECT * FROM OPENQUERY([" + safe + "], "  # nosec B608 - reviewed: only int-cast counts and dialect-quoted catalog identifiers interpolated, never raw user input (see SECURITY.md)
            "'SELECT CAST(SERVERPROPERTY(''ProductVersion'') AS varchar(50)) AS product_version, "
            "CAST(SERVERPROPERTY(''Edition'') AS varchar(100)) AS edition')")
        rows = cursor.fetchall()
        if rows:
            return _s(cell(rows[0], "product_version")), _s(cell(rows[0], "edition"))
    except Exception:
        return None
    return None


def collect_linked_servers(adapter, traverse: bool = False) -> LinkedServerReport:
    """Discover linked servers, map their security config, test connectivity, and
    optionally probe each reachable one for a health/version check. SQL Server
    only. Each step is isolated so a permission failure degrades gracefully."""
    report = LinkedServerReport(traversed=traverse)
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    try:
        try:
            cursor.execute("SELECT @@SERVERNAME AS server_name")
            rows = cursor.fetchall()
            report.local_server = _s(cell(rows[0], "server_name")) if rows else ""
        except Exception as e:
            report.errors.append(("Local server name", f"{type(e).__name__}: {e}"))

        try:
            servers = discover_linked_servers(cursor)
        except Exception as e:
            report.errors.append(("Discover linked servers", f"{type(e).__name__}: {e}"))
            servers = []

        logins_by_server = {}
        try:
            logins_by_server = get_linked_logins(cursor)
        except Exception as e:
            report.errors.append(("Linked logins", f"{type(e).__name__}: {e}"))

        for ls in servers:
            ls.logins = logins_by_server.get(ls.name, [])
            ls.reachable, ls.connectivity_message = probe_connectivity(cursor, ls.name)
            if traverse and ls.reachable:
                probe = probe_linked_server(cursor, ls.name)
                if probe:
                    ls.remote_version, ls.remote_edition = probe
        report.linked_servers = servers
    finally:
        conn.close()
    return report


def summarize_linked(report: LinkedServerReport) -> dict:
    servers = report.linked_servers
    return {
        "linked_servers": len(servers),
        "reachable": sum(1 for s in servers if s.reachable),
        "unreachable": sum(1 for s in servers if s.reachable is False),
        "rpc_out_enabled": sum(1 for s in servers if s.is_rpc_out),
        "data_access_enabled": sum(1 for s in servers if s.is_data_access),
    }


def collect_intel(database, tables, views=None, procedures=None, baseline_snapshot=None) -> IntelReport:
    report = IntelReport(database=database)
    report.naming_issues = analyze_naming(tables)
    report.orphan_fks = detect_orphan_fks(tables)
    report.impacts = analyze_impact(tables, views, procedures)
    if baseline_snapshot is not None:
        current = build_snapshot(database, tables, views, procedures)
        report.migration_sql = generate_migration(baseline_snapshot, current)
    return report


def summarize(report: IntelReport) -> dict:
    ls = report.linked_servers
    return {
        "naming_issues": len(report.naming_issues),
        "orphan_fks": len(report.orphan_fks),
        "high_impact_tables": sum(1 for i in report.impacts if i.total >= 3),
        "has_migration": bool(report.migration_sql),
        "linked_servers": len(ls.linked_servers) if ls else 0,
        "unreachable_linked_servers": (sum(1 for s in ls.linked_servers if s.reachable is False)
                                       if ls else 0),
    }
