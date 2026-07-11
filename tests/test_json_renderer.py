"""JSON export for `doc` (schema) and `scan` (findings)."""
import json

from sqldoc.json_renderer import build_json, render_json
from sqldoc.pii import findings_json, scan_tables
from sqldoc.extractor import Table, Column
from conftest import build_tables, build_views, build_procs


def test_build_json_full_model():
    data = build_json("DB", build_tables(), build_views(), build_procs())
    assert data["database"] == "DB"
    assert data["schema_version"] == 1
    assert data["stats"]["tables"] == 2
    assert data["stats"]["views"] == 1
    # full model incl. nested columns/indexes/triggers
    orders = next(t for t in data["tables"] if t["name"] == "Orders")
    assert orders["row_count"] == 1596
    assert {c["name"] for c in orders["columns"]} == {"Id", "CustomerID", "LineTotal", "Status"}
    lt = next(c for c in orders["columns"] if c["name"] == "LineTotal")
    assert lt["is_computed"] is True
    assert orders["indexes"][0]["name"] == "PK_Orders"
    assert orders["triggers"][0]["name"] == "trOrders"
    # constraints flow through asdict automatically
    assert orders["check_constraints"][0]["name"] == "CK_Orders_Status"
    assert orders["unique_constraints"][0]["columns"] == ["CustomerID"]
    cust = next(c for c in orders["columns"] if c["name"] == "CustomerID")
    assert cust["fk_on_delete"] == "CASCADE"


def test_render_json_roundtrips(tmp_path):
    out = tmp_path / "doc.json"
    render_json("DB", build_tables(), str(out), views=build_views(), procedures=build_procs())
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["database"] == "DB"
    assert len(data["tables"]) == 2


def test_findings_json_structure():
    findings = scan_tables([Table("dbo", "People", 3, columns=[
        Column("NationalID", "nvarchar", 20, True, False, False, None, None),
        Column("EmailAddress", "nvarchar", 100, True, False, False, None, None),
    ])])
    data = findings_json("DB", findings, sampled=False)
    assert data["database"] == "DB"
    assert data["summary"]["total"] == 2
    cats = {f["category"] for f in data["findings"]}
    assert "National ID / SSN" in cats and "Email Address" in cats
    # a Finding never carries sampled values
    assert not (set(data["findings"][0]) & {"sample", "samples", "values"})
