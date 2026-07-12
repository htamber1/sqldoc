"""Compliance expansion: per-regulation, lineage, access audit, and CLI."""
import json

from click.testing import CliRunner

from sqldoc import comply, cli
from sqldoc.comply import (build_regulation_sections, build_lineage, extract_permissions,
                           build_access_alerts, collect_compliance, classify_permission,
                           extract_role_members, build_principal_summary, Permission)
from sqldoc.comply_renderer import build_comply_json, render_comply_html
from sqldoc.pii import scan_tables
from sqldoc.extractor import Table, Column, View, StoredProcedure
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


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


def test_build_lineage_handles_dialect_quoting():
    # PostgreSQL/MySQL definitions quote identifiers differently; the INSERT
    # INTO detection must handle "..." and `...`, not just SQL Server [ ].
    orders = _table("orders", [_c("id", "int")], schema="public")
    archive = _table("archive", [_c("id", "int")], schema="public")
    proc_pg = StoredProcedure("public", "p_pg", parameters=[],
                              definition='INSERT INTO "archive" SELECT * FROM "orders"')
    proc_my = StoredProcedure("public", "p_my", parameters=[],
                              definition="INSERT INTO `archive` SELECT * FROM `orders`")
    for proc in (proc_pg, proc_my):
        flows = build_lineage([orders, archive], [], [proc])
        assert any(f.source == "public.orders" and f.target == "public.archive"
                   and f.kind == "procedure-write" for f in flows)


# --- access audit -----------------------------------------------------------

def test_extract_permissions(fake_permission_rows):
    perms = extract_permissions(FakeAdapter(FakeConnection(fake_permission_rows)))
    assert len(perms) == 3
    assert perms[0].principal == "app_reader" and perms[0].permission == "SELECT"


def test_extract_permissions_postgres(fake_pg_grant_rows):
    perms = extract_permissions(FakeAdapter(FakeConnection(fake_pg_grant_rows), dialect="postgres"))
    assert len(perms) == 2
    assert perms[0].state == "GRANT"          # table_privileges lists only grants
    assert perms[0].permission == "SELECT" and perms[0].object == "people"


def test_extract_permissions_unsupported_dialect_returns_empty():
    assert extract_permissions(FakeAdapter(FakeConnection({}), dialect="sqlite")) == []


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


# --- enhanced access audit: levels, roles, principal summary ----------------

def test_classify_permission_levels():
    assert classify_permission("SELECT") == "read"
    assert classify_permission("REFERENCES") == "read"
    assert classify_permission("INSERT") == "write"
    assert classify_permission("EXECUTE") == "write"
    assert classify_permission("DELETE") == "write"
    assert classify_permission("ALTER") == "admin"
    assert classify_permission("CONTROL") == "admin"
    # GRANT WITH GRANT OPTION escalates even a read permission to admin
    assert classify_permission("SELECT", "GRANT_WITH_GRANT_OPTION") == "admin"


def test_extract_role_members_sqlserver(fake_role_member_rows):
    members = extract_role_members(FakeAdapter(FakeConnection(fake_role_member_rows)))
    assert len(members) == 3
    assert members[0].role == "db_datareader" and members[0].member == "app_reader"


def test_extract_role_members_unsupported_dialect_returns_empty():
    assert extract_role_members(FakeAdapter(FakeConnection({}), dialect="mysql")) == []
    assert extract_role_members(FakeAdapter(FakeConnection({}), dialect="sqlite")) == []


def test_build_principal_summary_aggregates_levels_and_pii():
    findings = scan_tables([_table("People", [_c("NationalID")])])  # dbo.People HIGH PII
    perms = [
        Permission("app_reader", "SQL_USER", "SELECT", "GRANT", "dbo", "People", "USER_TABLE"),
        Permission("app_reader", "SQL_USER", "INSERT", "GRANT", "dbo", "People", "USER_TABLE"),
        Permission("app_reader", "SQL_USER", "SELECT", "GRANT", "dbo", "Products", "USER_TABLE"),
        Permission("analyst", "SQL_USER", "SELECT", "DENY", "dbo", "People", "USER_TABLE"),
    ]
    from sqldoc.comply import RoleMember
    roles = [RoleMember("db_datareader", "app_reader", "SQL_USER")]
    summary = {p.principal: p for p in build_principal_summary(perms, findings, roles)}

    reader = summary["app_reader"]
    assert reader.levels == ["read", "write"]           # SELECT + INSERT, ordered
    assert reader.object_count == 2                      # People + Products
    assert reader.pii_object_count == 1                  # only People holds PII
    assert reader.max_risk == "HIGH"

    # analyst's grant was DENY-only -> excluded entirely
    assert "analyst" not in summary
    # the role itself appears with its expanded members
    role = summary["db_datareader"]
    assert role.is_role and role.members == ["app_reader"]


# --- orchestration + degrade -----------------------------------------------

class _BoomAdapter:
    dialect = "sqlserver"
    def connect(self):
        raise PermissionError("VIEW DEFINITION denied")
    def cursor(self, conn):
        return conn.cursor()


def test_collect_compliance_degrades_without_permission():
    tables = [_table("People", [_c("NationalID")])]
    findings = scan_tables(tables)
    report = collect_compliance("DB", tables, findings, adapter=_BoomAdapter())
    assert report.permissions == []
    assert report.errors and "Access audit" in report.errors[0][0]
    assert report.regulations                        # regulation sections still built


# --- JSON + render ----------------------------------------------------------

def test_build_and_render(fake_permission_rows, fake_role_member_rows, tmp_path):
    tables = [_table("People", [_c("NationalID"), _c("CardNumber")])]
    findings = scan_tables(tables)
    # merge grants + role memberships into one fake connection
    rows = {**fake_permission_rows, **fake_role_member_rows}
    report = collect_compliance("DB", tables, findings,
                                adapter=FakeAdapter(FakeConnection(rows)))
    data = build_comply_json("DB", report)
    assert data["report_type"] == "compliance"
    assert data["summary"]["pci_dss"] >= 1
    assert any(a["table"] == "People" for a in data["access_alerts"])
    # new: unified per-principal view + expanded role membership
    assert data["summary"]["principals"] >= 1
    assert data["summary"]["roles"] >= 1
    assert any(p["principal"] == "app_reader" and "read" in p["levels"]
               for p in data["principals"])
    assert any(rm["role"] == "db_datareader" for rm in data["role_members"])

    out = tmp_path / "c.html"
    render_comply_html("DB", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "HIPAA" in h and "PCI-DSS" in h and "Data lineage" in h and "Access audit" in h
    assert "Access by principal" in h and "db_datareader" in h and "member(s)" in h


# --- CLI --------------------------------------------------------------------

def test_comply_cli(monkeypatch, fake_permission_rows, tmp_path):
    people = _table("People", [
        Column("Id", "int", 4, False, True, False, None, None),
        _c("NationalID"),
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda adapter: [people])
    monkeypatch.setattr(cli, "extract_views", lambda adapter: [])
    monkeypatch.setattr(cli, "extract_procedures", lambda adapter: [])
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_permission_rows)))
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
    monkeypatch.setattr(cli, "extract_metadata", lambda adapter: [people])
    monkeypatch.setattr(cli, "extract_views", lambda adapter: [])
    monkeypatch.setattr(cli, "extract_procedures", lambda adapter: [])
    # the adapter must NOT connect when --no-access-audit is set
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: (_ for _ in ()).throw(AssertionError("should not connect"))))
    res = CliRunner().invoke(cli.cli, [
        "comply", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-access-audit", "--output", str(tmp_path / "c.html"),
    ])
    assert res.exit_code == 0, res.output
    assert "Access alerts: 0" in res.output
