"""Schema change detection: snapshot the schema shape to JSON and diff two runs.

A snapshot captures only structural facts (object names, column types,
keys, indexes, parameters) — never AI descriptions or row data — so diffing
two runs reports genuine schema drift, like a git diff for your database.
"""
import json
import os

SNAPSHOT_VERSION = 1


def build_snapshot(database, tables, views=None, procedures=None) -> dict:
    views = views or []
    procedures = procedures or []

    def col_shape(c):
        shape = {"type": c.data_type, "nullable": bool(c.is_nullable)}
        if c.is_primary_key:
            shape["pk"] = True
        if c.is_foreign_key and c.references_table:
            shape["references"] = f"{c.references_table}.{c.references_column}"
            if getattr(c, "fk_on_delete", None):
                shape["on_delete"] = c.fk_on_delete
            if getattr(c, "fk_on_update", None):
                shape["on_update"] = c.fk_on_update
        if getattr(c, "default_definition", None):
            shape["default"] = c.default_definition
        return shape

    tables_out = {}
    for t in tables:
        indexes = {}
        for idx in t.indexes:
            indexes[idx.name] = {
                "type": idx.type_desc,
                "unique": bool(idx.is_unique),
                "primary_key": bool(idx.is_primary_key),
                "columns": list(idx.key_columns),
                "included": list(idx.included_columns),
            }
        tables_out[f"{t.schema}.{t.name}"] = {
            "row_count": t.row_count,
            "columns": {c.name: col_shape(c) for c in t.columns},
            "indexes": indexes,
            "checks": {chk.name: chk.definition for chk in getattr(t, "check_constraints", [])},
            "uniques": {uq.name: list(uq.columns) for uq in getattr(t, "unique_constraints", [])},
        }

    views_out = {
        f"{v.schema}.{v.name}": {"columns": {c.name: c.data_type for c in v.columns}}
        for v in views
    }
    procs_out = {
        f"{p.schema}.{p.name}": {"parameters": {pm.name: pm.data_type for pm in p.parameters}}
        for p in procedures
    }

    return {
        "version": SNAPSHOT_VERSION,
        "database": database,
        "tables": tables_out,
        "views": views_out,
        "procedures": procs_out,
    }


def save_snapshot(snapshot: dict, path: str):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)


def load_snapshot(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _diff_columns(old_cols: dict, new_cols: dict) -> dict:
    added = [name for name in new_cols if name not in old_cols]
    removed = [name for name in old_cols if name not in new_cols]
    changed = []
    for name in new_cols:
        if name not in old_cols:
            continue
        o, n = old_cols[name], new_cols[name]
        deltas = []
        if o.get("type") != n.get("type"):
            deltas.append(("type", o.get("type"), n.get("type")))
        if o.get("nullable") != n.get("nullable"):
            deltas.append(("nullable", o.get("nullable"), n.get("nullable")))
        if bool(o.get("pk")) != bool(n.get("pk")):
            deltas.append(("pk", bool(o.get("pk")), bool(n.get("pk"))))
        if o.get("default") != n.get("default"):
            deltas.append(("default", o.get("default"), n.get("default")))
        if o.get("on_delete") != n.get("on_delete"):
            deltas.append(("on_delete", o.get("on_delete"), n.get("on_delete")))
        if o.get("on_update") != n.get("on_update"):
            deltas.append(("on_update", o.get("on_update"), n.get("on_update")))
        if deltas:
            changed.append({"name": name, "deltas": deltas,
                            "new_type": n.get("type"), "old_type": o.get("type")})
    return {"added": sorted(added), "removed": sorted(removed), "changed": changed}


def diff_snapshots(old: dict, new: dict) -> dict:
    old_t, new_t = old.get("tables", {}), new.get("tables", {})

    tables_added = sorted(name for name in new_t if name not in old_t)
    tables_removed = sorted(name for name in old_t if name not in new_t)

    def _presence_pair(old_map, new_map):
        return (sorted(k for k in new_map if k not in old_map),
                sorted(k for k in old_map if k not in new_map))

    tables_modified = []
    for name in sorted(set(old_t) & set(new_t)):
        cd = _diff_columns(old_t[name].get("columns", {}), new_t[name].get("columns", {}))
        checks_added, checks_removed = _presence_pair(
            old_t[name].get("checks", {}), new_t[name].get("checks", {}))
        uniques_added, uniques_removed = _presence_pair(
            old_t[name].get("uniques", {}), new_t[name].get("uniques", {}))
        if (cd["added"] or cd["removed"] or cd["changed"]
                or checks_added or checks_removed or uniques_added or uniques_removed):
            tables_modified.append({
                "name": name, **cd,
                "checks_added": checks_added, "checks_removed": checks_removed,
                "uniques_added": uniques_added, "uniques_removed": uniques_removed,
            })

    def presence(kind):
        o, n = old.get(kind, {}), new.get(kind, {})
        return (sorted(x for x in n if x not in o),
                sorted(x for x in o if x not in n))

    views_added, views_removed = presence("views")
    procs_added, procs_removed = presence("procedures")

    diff = {
        "database": new.get("database"),
        "tables_added": tables_added,
        "tables_removed": tables_removed,
        "tables_modified": tables_modified,
        "views_added": views_added,
        "views_removed": views_removed,
        "procedures_added": procs_added,
        "procedures_removed": procs_removed,
    }
    diff["counts"] = {
        "added": len(tables_added),
        "removed": len(tables_removed),
        "modified": len(tables_modified),
    }
    diff["has_changes"] = any([
        tables_added, tables_removed, tables_modified,
        views_added, views_removed, procs_added, procs_removed,
    ])
    # New tables' column counts, for a richer "+ table (N columns)" line.
    diff["_new_tables"] = new_t
    return diff


def iter_diff_lines(diff: dict):
    """Yield (kind, text) pairs for rendering. kind in:
    header, add, remove, change, context, summary, none."""
    if not diff["has_changes"]:
        yield ("none", "No schema changes since the last snapshot.")
        return

    new_t = diff.get("_new_tables", {})

    for name in diff["tables_added"]:
        ncols = len(new_t.get(name, {}).get("columns", {}))
        yield ("add", f"+ table    {name}  ({ncols} columns)")
    for name in diff["tables_removed"]:
        yield ("remove", f"- table    {name}")

    for mod in diff["tables_modified"]:
        yield ("change", f"~ table    {mod['name']}")
        for col in mod["added"]:
            yield ("add", f"    + column   {col}")
        for col in mod["removed"]:
            yield ("remove", f"    - column   {col}")
        for ch in mod["changed"]:
            for field, old_v, new_v in ch["deltas"]:
                yield ("change", f"    ~ column   {ch['name']}: {field} {old_v} -> {new_v}")
        for c in mod.get("checks_added", []):
            yield ("add", f"    + check    {c}")
        for c in mod.get("checks_removed", []):
            yield ("remove", f"    - check    {c}")
        for u in mod.get("uniques_added", []):
            yield ("add", f"    + unique   {u}")
        for u in mod.get("uniques_removed", []):
            yield ("remove", f"    - unique   {u}")

    for name in diff["views_added"]:
        yield ("add", f"+ view     {name}")
    for name in diff["views_removed"]:
        yield ("remove", f"- view     {name}")
    for name in diff["procedures_added"]:
        yield ("add", f"+ proc     {name}")
    for name in diff["procedures_removed"]:
        yield ("remove", f"- proc     {name}")

    c = diff["counts"]
    parts = []
    n_add = len(diff["tables_added"]); n_rem = len(diff["tables_removed"])
    if n_add:
        parts.append(f"{n_add} table(s) added")
    if n_rem:
        parts.append(f"{n_rem} table(s) removed")
    if c["modified"]:
        parts.append(f"{c['modified']} table(s) modified")
    vp = (len(diff["views_added"]) + len(diff["views_removed"])
          + len(diff["procedures_added"]) + len(diff["procedures_removed"]))
    if vp:
        parts.append(f"{vp} view/proc change(s)")
    yield ("summary", "Schema changes: " + ", ".join(parts))


def format_diff(diff: dict) -> str:
    """Plain-text (no color) rendering, e.g. for logs or tests."""
    return "\n".join(text for _, text in iter_diff_lines(diff))
