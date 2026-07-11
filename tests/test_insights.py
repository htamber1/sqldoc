"""AI-powered insights: anomalies, relationships, glossary, NL->SQL, and CLI."""
import json

import pytest
from click.testing import CliRunner

import sqldoc.ai as ai
from sqldoc import insights, cli
from sqldoc.insights import (detect_anomalies, infer_relationships, answer_question,
                             generate_glossary, collect_insights)
from sqldoc.insights_renderer import build_insights_json, render_insights_html
from sqldoc.extractor import Table, Column, View, StoredProcedure
from conftest import build_tables, build_views, build_procs


def _t(schema, name, cols, **kw):
    return Table(schema, name, kw.get("rows", 1), columns=cols)


def _c(name, dt="int", pk=False, fk=False, rt=None, rc=None):
    return Column(name, dt, 4, True, pk, fk, rt, rc)


# --- anomaly detection (heuristic) -----------------------------------------

def test_anomaly_no_primary_key():
    t = _t("dbo", "Log", [_c("Message", "nvarchar"), _c("Level", "int")])
    kinds = {(a.kind, a.object) for a in detect_anomalies([t])}
    assert ("no-primary-key", "dbo.Log") in kinds


def test_anomaly_generic_name_and_date_as_string():
    t = _t("dbo", "Thing", [
        _c("Id", pk=True),
        _c("Data", "nvarchar"),               # generic name
        _c("OrderDate", "varchar"),           # date stored as string
        _c("TotalAmount", "varchar"),         # number stored as string
    ])
    kinds = {(a.kind, a.object) for a in detect_anomalies([t])}
    assert ("generic-name", "dbo.Thing.Data") in kinds
    assert ("date-as-string", "dbo.Thing.OrderDate") in kinds
    assert ("number-as-string", "dbo.Thing.TotalAmount") in kinds


def test_anomaly_missing_audit_columns():
    t = _t("dbo", "Product", [_c("Id", pk=True), _c("Name", "nvarchar"), _c("Price", "money")])
    kinds = {a.kind for a in detect_anomalies([t])}
    assert "missing-audit-columns" in kinds
    # a table WITH audit columns is not flagged
    t2 = _t("dbo", "Order", [_c("Id", pk=True), _c("Total", "money"), _c("CreatedDate", "datetime")])
    assert "missing-audit-columns" not in {a.kind for a in detect_anomalies([t2])}


def test_anomaly_severity_sort():
    t = _t("dbo", "Bad", [_c("Data", "nvarchar")])   # no PK (HIGH) + generic (MEDIUM) + audit (LOW)
    sevs = [a.severity for a in detect_anomalies([t])]
    assert sevs == sorted(sevs, key=lambda s: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[s])


# --- relationship inference -------------------------------------------------

def test_infer_relationships():
    customer = _t("dbo", "Customer", [_c("Id", pk=True)])
    orders = _t("dbo", "Orders", [_c("Id", pk=True), _c("CustomerID", "int"), _c("SupplierID", "int")])
    rels = infer_relationships([customer, orders])
    by_col = {r.from_column: r for r in rels}
    assert "CustomerID" in by_col                      # Customer exists
    assert "SupplierID" not in by_col                  # no Supplier table
    r = by_col["CustomerID"]
    assert r.to_table == "dbo.Customer" and r.to_column == "Id"
    assert r.confidence >= 0.8                          # name + type match
    assert "ADD CONSTRAINT [FK_Orders_CustomerID] FOREIGN KEY ([CustomerID])" in r.ddl


def test_infer_relationships_skips_existing_fk():
    customer = _t("dbo", "Customer", [_c("Id", pk=True)])
    orders = _t("dbo", "Orders", [_c("Id", pk=True), _c("CustomerID", "int", fk=True, rt="Customer", rc="Id")])
    assert infer_relationships([customer, orders]) == []


# --- AI parts (mocked) ------------------------------------------------------

def test_answer_question(monkeypatch):
    monkeypatch.setattr(ai, "_call_ollama",
                        lambda p, m: "```sql\nSELECT * FROM Sales.Orders;\n```")
    qr = answer_question("show all orders", build_tables(), mode="local")
    assert qr.question == "show all orders"
    assert qr.sql == "SELECT * FROM Sales.Orders;"      # fence stripped


def test_answer_question_prompt_has_schema_no_rows(monkeypatch):
    captured = {}
    monkeypatch.setattr(ai, "_call_ollama", lambda p, m: captured.setdefault("p", p) or "SELECT 1;")
    answer_question("how many orders?", build_tables(), mode="local")
    assert "Sales.Orders(" in captured["p"]              # schema context present
    assert "row data" not in captured["p"].lower() or "ONLY the schema" in captured["p"]


def test_generate_glossary(monkeypatch):
    monkeypatch.setattr(ai, "_call_ollama", lambda p, m: "Stores customer sales orders.")
    entries = generate_glossary(build_tables(), mode="local", concurrency=2)
    assert len(entries) == 2
    terms = {e.term for e in entries}
    assert "Orders" in terms and "Archive" in terms
    assert all(e.definition for e in entries)


def test_collect_insights_no_ai_skips_ai_parts():
    report = collect_insights("DB", build_tables(), questions=["x"], use_ai=False)
    assert report.queries == [] and report.glossary == []
    assert report.anomalies or report.relationships is not None   # heuristics still run


# --- JSON + render ----------------------------------------------------------

def test_build_and_render(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_call_ollama", lambda p, m: "A definition.")
    report = collect_insights("DB", build_tables(), questions=[], use_ai=True,
                              glossary=True, mode="local", concurrency=2)
    data = build_insights_json("DB", report)
    assert data["report_type"] == "insights"
    assert data["summary"]["glossary_terms"] == 2

    out = tmp_path / "i.html"
    render_insights_html("DB", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "AI Insights" in h and "Schema anomalies" in h and "Business glossary" in h


# --- CLI --------------------------------------------------------------------

def test_insights_cli_no_ai(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: build_tables())
    out = tmp_path / "i.html"
    jout = tmp_path / "i.json"
    res = CliRunner().invoke(cli.cli, [
        "insights", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-ai", "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Anomalies:" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["report_type"] == "insights"
    assert data["summary"]["glossary_terms"] == 0        # AI skipped


def test_insights_cli_with_ai_and_ask(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: build_tables())
    monkeypatch.setattr(ai, "_call_ollama", lambda p, m: "SELECT 1;")
    out = tmp_path / "i.html"
    res = CliRunner().invoke(cli.cli, [
        "insights", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--ask", "how many orders?", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert "Queries: 1" in res.output


def test_insights_cli_cloud_aborts_without_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: build_tables())
    res = CliRunner().invoke(cli.cli, [
        "insights", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--mode", "cloud", "--output", str(tmp_path / "i.html"),
    ], input="n\n")
    assert res.exit_code != 0
    assert "Aborted" in res.output
