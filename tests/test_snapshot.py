"""Schema change detection: snapshot building + diff logic."""
from sqldoc import snapshot
from conftest import build_tables, build_views, build_procs


def test_build_snapshot_is_structure_only():
    snap = snapshot.build_snapshot("DB", build_tables(), build_views(), build_procs())
    assert snap["database"] == "DB"
    assert "Sales.Orders" in snap["tables"]
    cols = snap["tables"]["Sales.Orders"]["columns"]
    assert cols["Id"]["pk"] is True
    assert cols["CustomerID"]["references"] == "Customer.Id"
    # descriptions are never part of a snapshot
    assert "description" not in cols["Id"]
    assert "Sales.vActiveOrders" in snap["views"]
    assert "Sales.uspGetOrder" in snap["procedures"]


def test_diff_no_changes():
    snap = snapshot.build_snapshot("DB", build_tables(), build_views(), build_procs())
    diff = snapshot.diff_snapshots(snap, snap)
    assert diff["has_changes"] is False
    assert snapshot.format_diff(diff).startswith("No schema changes")


def test_diff_detects_all_categories():
    old = snapshot.build_snapshot("DB", build_tables())
    new_tables = build_tables()
    # drop a table
    new_tables = [t for t in new_tables if t.name != "Archive"]
    # add a column, drop a column, change a type on Orders
    orders = new_tables[0]
    from sqldoc.extractor import Column
    orders.columns.append(Column("Note", "nvarchar", 200, True, False, False, None, None))
    orders.columns = [c for c in orders.columns if c.name != "LineTotal"]
    orders.columns[1].data_type = "bigint"   # CustomerID int -> bigint
    new = snapshot.build_snapshot("DB", new_tables)

    diff = snapshot.diff_snapshots(old, new)
    assert diff["has_changes"] is True
    assert diff["tables_removed"] == ["Sales.Archive"]

    mod = next(m for m in diff["tables_modified"] if m["name"] == "Sales.Orders")
    assert "Note" in mod["added"]
    assert "LineTotal" in mod["removed"]
    changed = {c["name"] for c in mod["changed"]}
    assert "CustomerID" in changed


def test_snapshot_captures_constraints():
    snap = snapshot.build_snapshot("DB", build_tables())
    orders = snap["tables"]["Sales.Orders"]
    assert "CK_Orders_Status" in orders["checks"]
    assert orders["uniques"]["UQ_Orders_Customer"] == ["CustomerID"]
    assert orders["columns"]["Status"]["default"] == "((0))"
    assert orders["columns"]["CustomerID"]["on_delete"] == "CASCADE"


def test_diff_detects_constraint_changes():
    from sqldoc.extractor import CheckConstraint
    old = snapshot.build_snapshot("DB", build_tables())
    new_tables = build_tables()
    orders = new_tables[0]
    orders.check_constraints = []              # drop the check constraint
    orders.unique_constraints = []             # drop the unique constraint
    orders.columns[3].default_definition = "((1))"   # Status default 0 -> 1
    new = snapshot.build_snapshot("DB", new_tables)
    diff = snapshot.diff_snapshots(old, new)
    mod = next(m for m in diff["tables_modified"] if m["name"] == "Sales.Orders")
    assert "CK_Orders_Status" in mod["checks_removed"]
    assert "UQ_Orders_Customer" in mod["uniques_removed"]
    changed = {c["name"] for c in mod["changed"]}
    assert "Status" in changed
    text = snapshot.format_diff(diff)
    assert "- check" in text and "- unique" in text


def test_diff_detects_added_table_and_view():
    old = snapshot.build_snapshot("DB", build_tables()[:1])   # only Orders
    new = snapshot.build_snapshot("DB", build_tables(), build_views())
    diff = snapshot.diff_snapshots(old, new)
    assert "Sales.Archive" in diff["tables_added"]
    assert "Sales.vActiveOrders" in diff["views_added"]
    text = snapshot.format_diff(diff)
    assert "+ table" in text and "+ view" in text
