"""Query-plan analyzer: XML pattern parsing, dialects, AI, render, CLI."""
import json

from click.testing import CliRunner

from sqldoc import cli, plans as plans_mod
from sqldoc.plans import collect_plans, parse_plan_xml, explain_plans, summarize
from sqldoc.plans_renderer import build_plans_json, render_plans_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter, _PLAN_XML


# --- XML pattern parsing ----------------------------------------------------

def test_parse_plan_xml_patterns():
    pats = {p.kind: p for p in parse_plan_xml(_PLAN_XML)}
    assert "missing-index" in pats and "87.5" in pats["missing-index"].detail
    assert pats["table-scan"].severity == "HIGH"      # 500k rows
    assert "key-lookup" in pats
    assert "large-scan" in pats                        # clustered index scan over 50k
    assert "spill" in pats
    assert pats["implicit-conversion"].severity == "HIGH"   # PlanAffectingConvert


def test_parse_plan_xml_empty():
    assert parse_plan_xml("") == []
    assert parse_plan_xml("<ShowPlanXML/>") == []
    assert parse_plan_xml("not xml") == []


# --- SQL Server -------------------------------------------------------------

def test_collect_sqlserver_plans(fake_mssql_plans_rows):
    report = collect_plans(FakeAdapter(FakeConnection(fake_mssql_plans_rows), dialect="sqlserver"))
    assert report.has_plan_xml and len(report.plans) == 2
    top = report.plans[0]
    assert top.avg_elapsed_ms == 1200.5 and top.severity == "HIGH"
    assert any(p.kind == "missing-index" for p in top.patterns)
    assert report.plans[1].patterns == []             # trivial plan, no patterns
    s = summarize(report)
    assert s["plans"] == 2 and s["high_severity"] == 1
    assert "missing-index" in s["pattern_counts"]


# --- PostgreSQL / MySQL -----------------------------------------------------

def test_collect_postgres_plans(fake_pg_plans_rows):
    report = collect_plans(FakeAdapter(FakeConnection(fake_pg_plans_rows), dialect="postgres"))
    assert not report.has_plan_xml and len(report.plans) == 1
    assert report.plans[0].avg_elapsed_ms == 90.0
    assert report.notes                                # PG plan-XML caveat


def test_collect_mysql_plans_no_index(fake_mysql_plans_rows):
    report = collect_plans(FakeAdapter(FakeConnection(fake_mysql_plans_rows), dialect="mysql"))
    assert len(report.plans) == 1
    assert any(p.kind == "no-index" for p in report.plans[0].patterns)   # SUM_NO_INDEX_USED > 0


def test_unsupported_dialect():
    report = collect_plans(FakeAdapter(FakeConnection({}), dialect="sqlite"))
    assert not report.supported


# --- AI + render + json + CLI -----------------------------------------------

def test_explain_plans(monkeypatch, fake_mssql_plans_rows):
    captured = {}

    def fake_ai(p, m, mo):
        captured["p"] = p
        return "CREATE INDEX IX_Orders_CustomerId ON Sales.Orders(CustomerId) INCLUDE (Total);"

    monkeypatch.setattr(plans_mod, "_ai_call", fake_ai)
    report = collect_plans(FakeAdapter(FakeConnection(fake_mssql_plans_rows), dialect="sqlserver"))
    explain_plans(report, mode="local", limit=1)
    assert "CREATE INDEX" in report.plans[0].ai_explanation
    assert "missing-index" in captured["p"]            # patterns sent to model
    assert report.plans[1].ai_explanation == ""        # limit=1


def test_build_and_render(fake_mssql_plans_rows, tmp_path):
    report = collect_plans(FakeAdapter(FakeConnection(fake_mssql_plans_rows), dialect="sqlserver"))
    report.plans[0].ai_explanation = "Add a covering index."
    data = build_plans_json("SRV", report)
    assert data["report_type"] == "plans" and data["summary"]["high_severity"] == 1
    assert data["plans"][0]["severity"] == "HIGH"

    out = tmp_path / "p.html"
    render_plans_html("SRV", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Query Plans" in h and "missing-index" in h and "AI recommendation" in h


def test_plans_cli(monkeypatch, fake_mssql_plans_rows, tmp_path):
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_mssql_plans_rows)))
    monkeypatch.setattr(plans_mod, "_ai_call", lambda p, m, mo: "Add an index.")
    out = tmp_path / "p.html"
    jout = tmp_path / "p.json"
    res = CliRunner().invoke(cli.cli, [
        "plans", "--server", "h", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Plans: 2" in res.output and "High-severity: 1" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["plans"][0]["ai_explanation"] == "Add an index."
