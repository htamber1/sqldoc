"""Schema intelligence: naming, orphaned FKs, impact, migrations, and CLI."""
import json

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.intel import (classify_case, analyze_naming, detect_orphan_fks,
                          analyze_impact, generate_migration, collect_intel)
from sqldoc.intel_renderer import build_intel_json, render_intel_html
from sqldoc.snapshot import build_snapshot
from sqldoc.extractor import Table, Column, View, StoredProcedure
from conftest import build_tables, build_views, build_procs


def _t(schema, name, cols):
    return Table(schema, name, 1, columns=cols)


def _c(name, dt="int", pk=False, fk=False, rt=None, rc=None):
    return Column(name, dt, 4, True, pk, fk, rt, rc)


def test_classify_case():
    assert classify_case("CustomerOrders") == "Pascal"
    assert classify_case("customer_orders") == "snake"
    assert classify_case("customerOrders") == "camel"
    assert classify_case("CUSTOMER") == "UPPER"


def test_analyze_naming_flags_case_outlier():
    tables = [_t("dbo", "Customers", [_c("Id", pk=True)]),
              _t("dbo", "Orders", [_c("Id", pk=True)]),
              _t("dbo", "order_items", [_c("Id", pk=True)])]
    kinds = {(i.kind, i.object) for i in analyze_naming(tables)}
    assert ("table-case", "order_items") in kinds


def test_detect_orphan_fks():
    tables = [_t("dbo", "Customer", [_c("Id", pk=True)]),
              _t("dbo", "Orders", [_c("Id", pk=True), _c("CustomerID"), _c("SupplierID")])]
    orphans = detect_orphan_fks(tables)
    cols = {o.column for o in orphans}
    assert "CustomerID" in cols          # a Customer table exists
    assert "SupplierID" not in cols      # no Supplier table -> not flagged
    assert all(o.implied_table == "Customer" for o in orphans)


def test_detect_orphan_fks_ignores_real_fks_and_pk():
    tables = [_t("dbo", "Customer", [_c("Id", pk=True)]),
              _t("dbo", "Orders", [_c("Id", pk=True),
                                   _c("CustomerID", fk=True, rt="Customer", rc="Id")])]
    assert detect_orphan_fks(tables) == []


def test_analyze_impact():
    customer = _t("dbo", "Customer", [_c("Id", pk=True)])
    orders = _t("dbo", "Orders", [_c("Id", pk=True),
                                  _c("CustomerID", fk=True, rt="Customer", rc="Id")])
    view = View("dbo", "vCust", columns=[], definition="SELECT * FROM Customer")
    proc = StoredProcedure("dbo", "pGetCust", parameters=[], definition="SELECT Id FROM Customer")
    imp = next(i for i in analyze_impact([customer, orders], [view], [proc]) if i.table == "Customer")
    assert "dbo.Orders.CustomerID" in imp.fk_dependents
    assert "dbo.vCust" in imp.view_dependents
    assert "dbo.pGetCust" in imp.proc_dependents
    assert imp.total >= 3


def test_generate_migration():
    old = build_snapshot("DB", build_tables())
    new_tables = [t for t in build_tables() if t.name != "Archive"]     # drop Archive
    new_tables[0].columns.append(Column("Note", "nvarchar", 200, True, False, False, None, None))
    new = build_snapshot("DB", new_tables)
    sql = generate_migration(old, new)
    assert "ALTER TABLE [Sales].[Orders] ADD [Note] nvarchar NULL" in sql
    assert "DROP TABLE [Sales].[Archive];" in sql


def test_generate_migration_no_changes():
    snap = build_snapshot("DB", build_tables())
    assert "No schema changes" in generate_migration(snap, snap)


def test_collect_intel_with_baseline_generates_migration():
    base = build_snapshot("DB", build_tables()[:1])        # only Orders
    report = collect_intel("DB", build_tables())
    assert report.migration_sql == ""                       # no baseline -> no migration
    report2 = collect_intel("DB", build_tables(), baseline_snapshot=base)
    assert "CREATE TABLE [Sales].[Archive]" in report2.migration_sql


def test_build_intel_json_and_render(tmp_path):
    report = collect_intel("DB", build_tables(), views=build_views(), procedures=build_procs())
    data = build_intel_json("DB", report)
    assert data["report_type"] == "intel"
    # the view definition mentions Sales.Orders -> Orders has a view dependent
    orders_imp = next(i for i in data["impacts"] if i["table"] == "Orders")
    assert "Sales.vActiveOrders" in orders_imp["view_dependents"]

    out = tmp_path / "i.html"
    render_intel_html("DB", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Schema Intelligence" in h and "Impact analysis" in h


def test_intel_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: build_tables())
    monkeypatch.setattr(cli, "extract_views", lambda cs: build_views())
    monkeypatch.setattr(cli, "extract_procedures", lambda cs: build_procs())
    out = tmp_path / "i.html"
    jout = tmp_path / "i.json"
    res = CliRunner().invoke(cli.cli, [
        "intel", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Naming issues:" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["report_type"] == "intel"


def test_intel_cli_migration_out(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: build_tables())
    monkeypatch.setattr(cli, "extract_views", lambda cs: build_views())
    monkeypatch.setattr(cli, "extract_procedures", lambda cs: build_procs())
    # baseline with only Orders -> Archive shows up as an added table
    base = build_snapshot("DB", build_tables()[:1])
    base_path = tmp_path / "base.json"
    from sqldoc.snapshot import save_snapshot
    save_snapshot(base, str(base_path))
    mig = tmp_path / "migration.sql"
    res = CliRunner().invoke(cli.cli, [
        "intel", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--baseline", str(base_path), "--migration-out", str(mig),
        "--output", str(tmp_path / "i.html"),
    ])
    assert res.exit_code == 0, res.output
    assert "Migration: generated" in res.output
    assert "CREATE TABLE [Sales].[Archive]" in mig.read_text(encoding="utf-8")
