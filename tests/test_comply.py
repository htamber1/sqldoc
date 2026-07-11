"""Compliance expansion: per-regulation, lineage, access audit, and CLI."""
import json

from click.testing import CliRunner

from sqldoc import comply, cli
from sqldoc.comply import (build_regulation_sections, build_lineage, extract_permissions,
                           build_access_alerts, collect_compliance)
from sqldoc.comply_renderer import build_comply_json, render_comply_html
from sqldoc.pii import scan_tables
from sqldoc.extractor import Table, Column, View, StoredProcedure
from conftest import FakeConnection


def _table(name, cols, schema="dbo"):
    return Table(schema, name, 1, columns=cols)


def _c(name, dt="nvarchar"):
    return Column(name, dt, 50, True, False, False, None, None)


# --- per-regulation ---------------------------------------------------------

def test_build_regulation_sections():
    findings = scan_tables([_table("People", [
        _c("NationalID"),        # GDPR + HIPAA, HIGH
        _c("CardNumber"),        # PCI-DSS, HIGH
        _c("EmailAddress"),      # GDPR, MEDIUM
    ])])
    secs = {s.regulation: s for s in build_regulation_sections(findings)}
    assert set(secs) == {"HIPAA", "GDPR", "PCI-DSS"}
    assert secs["PCI-DSS"].column_count == 1                 # CardNumber
    assert secs["GDPR"].column_count >= 2                    # NationalID + Email
    assert secs["HIPAA"].high_count == 1                     # NationalID
    assert secs["HIPAA"].controls                            # controls attached
    # HIGH sorted before MEDIUM within a section
    gdpr_risks = [f.risk for f in secs["GDPR"].findings]
    assert gdpr_risks == sorted(gdpr_risks, key=lambda r: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[r])


# --- lineage ----------------------------------------------------------------

def test_build_lineage_view_and_proc_write():
    orders = _table("Orders", [_c("Id", "int")])
    archive = _table("Archive", [_c("Id", "int")])
    view = View("dbo", "vOrders", columns=[], definition="SELECT Id FROM Orders")
    proc = StoredProcedure("dbo", "pArchive", parameters=[],
                           definition="INSERT INTO Archive SELECT * FROM Orders")
    flows = build_lineage([orders, archive], [view], [proc])
    kinds = {(f.source, f.target, f.kind) for f in flows}
    assert ("dbo.Orders", "dbo.vOrders", "view") in kinds
    assert ("dbo.Orders", "dbo.Archive", "procedure-write") in kinds


# --- access audit -----------------------------------------------------------

def test_extract_permissions(fake_permission_rows):
    conn = FakeConnection(fake_permission_rows)
    import sqldoc.comply as c
    # extract_permissions calls get_connection(cs); patch it to our fake
    orig = c.get_connection
    c.get_connection = lambda cs: conn
    try:
        perms = extract_permissions("cs")
    finally:
        c.get_connection = orig
    assert len(perms) == 3
    assert perms[0].principal == "app_reader" and perms[0].permission == "SELECT"


def test_build_access_alerts_flags_grants_on_pii_tables():
    findings = scan_tables([_table("People", [_c("NationalID")])])   # dbo.People has HIGH PII
    from sqldoc.comply import Permission
    perms = [
        Permission("app_reader", "SQL_USER", "SELECT", "GRANT", "dbo", "People", "USER_TABLE"),
        Permission("analyst", "SQL_USER", "SELECT", "DENY", "dbo", "People", "USER_TABLE"),
        Permission("app_reader", "SQL_USER", "SELECT", "GRANT", "dbo", "Products", "USER_TABLE"),
    ]
    alerts = build_access_alerts(perms, findings)
    assert len(alerts) == 1                          # DENY ignored, Products has no PII
    a = alerts[0]
    assert a.principal == "app_reader" and a.table == "People"
    assert a.max_risk == "HIGH"
    assert "National ID / SSN" in a.categories


# --- orchestration + degrade -----------------------------------------------

def test_collect_compliance_degrades_without_permission(monkeypatch):
    def boom(cs):
        raise PermissionError("VIEW DEFINITION denied")
    monkeypatch.setattr(comply, "get_connection", boom)
    tables = [_table("People", [_c("NationalID")])]
    findings = scan_tables(tables)
    report = collect_compliance("DB", tables, findings, connection_string="cs")
    assert report.permissions == []
    assert report.errors and "Access audit" in report.errors[0][0]
    assert report.regulations                        # regulation sections still built


# --- JSON + render ----------------------------------------------------------

def test_build_and_render(monkeypatch, fake_permission_rows, tmp_path):
    monkeypatch.setattr(comply, "get_connection", lambda cs: FakeConnection(fake_permission_rows))
    tables = [_table("People", [_c("NationalID"), _c("CardNumber")])]
    findings = scan_tables(tables)
    report = collect_compliance("DB", tables, findings, connection_string="cs")
    data = build_comply_json("DB", report)
    assert data["report_type"] == "compliance"
    assert data["summary"]["pci_dss"] >= 1
    assert any(a["table"] == "People" for a in data["access_alerts"])

    out = tmp_path / "c.html"
    render_comply_html("DB", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "HIPAA" in h and "PCI-DSS" in h and "Data lineage" in h and "Access audit" in h


# --- CLI --------------------------------------------------------------------

def test_comply_cli(monkeypatch, fake_permission_rows, tmp_path):
    people = _table("People", [
        Column("Id", "int", 4, False, True, False, None, None),
        _c("NationalID"),
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [people])
    monkeypatch.setattr(cli, "extract_views", lambda cs: [])
    monkeypatch.setattr(cli, "extract_procedures", lambda cs: [])
    monkeypatch.setattr(comply, "get_connection", lambda cs: FakeConnection(fake_permission_rows))
    out = tmp_path / "c.html"
    jout = tmp_path / "c.json"
    res = CliRunner().invoke(cli.cli, [
        "comply", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "HIPAA:" in res.output and "Access alerts:" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["report_type"] == "compliance"
    assert any(a["table"] == "People" for a in data["access_alerts"])


def test_comply_cli_no_access_audit(monkeypatch, tmp_path):
    people = _table("People", [_c("NationalID")])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [people])
    monkeypatch.setattr(cli, "extract_views", lambda cs: [])
    monkeypatch.setattr(cli, "extract_procedures", lambda cs: [])
    # get_connection must NOT be called when --no-access-audit is set
    monkeypatch.setattr(comply, "get_connection",
                        lambda cs: (_ for _ in ()).throw(AssertionError("should not connect")))
    res = CliRunner().invoke(cli.cli, [
        "comply", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-access-audit", "--output", str(tmp_path / "c.html"),
    ])
    assert res.exit_code == 0, res.output
    assert "Access alerts: 0" in res.output
