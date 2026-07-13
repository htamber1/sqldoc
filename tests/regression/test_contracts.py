"""Phase 10 — regression suite. Locks the public output contracts (CLI command
inventory, PII detection results, JSON key schemas, control numbers, DDL shape)
against a fixed input so an unintended change fails loudly. Deterministic: uses
the mock harness, no live database. Run after every release."""
import re

import pytest

from sqldoc.extractor import Table, Column

pytestmark = pytest.mark.regression


# --- fixed inputs ----------------------------------------------------------

def _person_table():
    return Table("dbo", "Person", 10, [
        Column("SSN", "varchar", 11, True, False, False, None, None),
        Column("Email", "varchar", 100, True, False, False, None, None),
        Column("Phone", "varchar", 20, True, False, False, None, None),
        Column("CreditCardNumber", "varchar", 20, True, False, False, None, None),
        Column("DateOfBirth", "date", 3, True, False, False, None, None),
    ])


# --- CLI command inventory -------------------------------------------------

EXPECTED_COMMANDS = {
    "access", "agent", "audit", "azuredevops", "azuredevops-wiki", "baseline",
    "box", "capacity", "cms", "comply", "confluence", "dbt", "deadlocks", "doc",
    "dropbox", "executive", "gdrive", "github-wiki", "gitlab-wiki", "ha", "health",
    "insights", "install-hooks", "intel", "jira", "logs", "notion", "nuclino",
    "onedrive", "plans", "powerbi", "quality", "scan", "scan-files", "secure",
    "serve", "server", "servicenow", "sharepoint", "waits", "webhook",
}
EXPECTED_ACCESS = {
    "approve", "check", "execute", "intake", "jira", "parse-email", "recommend",
    "request", "review", "script",
}


def test_cli_command_inventory():
    from sqldoc.cli import cli
    assert set(cli.commands) == EXPECTED_COMMANDS, \
        f"CLI command set changed: added={set(cli.commands) - EXPECTED_COMMANDS}, " \
        f"removed={EXPECTED_COMMANDS - set(cli.commands)}"
    assert set(cli.commands["access"].commands) == EXPECTED_ACCESS


# --- PII detection contract ------------------------------------------------

EXPECTED_PII = {
    "SSN": ("National ID / SSN", "HIGH"),
    "CreditCardNumber": ("Payment Card", "HIGH"),
    "Email": ("Email Address", "MEDIUM"),
    "Phone": ("Phone Number", "MEDIUM"),
    "DateOfBirth": ("Date of Birth", "MEDIUM"),
}


def test_pii_detection_contract():
    from sqldoc.pii import scan_tables, summarize
    findings = {f.column: (f.category, f.risk) for f in scan_tables([_person_table()])}
    for col, expected in EXPECTED_PII.items():
        assert findings.get(col) == expected, f"{col}: {findings.get(col)} != {expected}"
    assert summarize(scan_tables([_person_table()]))["by_risk"] == {"HIGH": 2, "MEDIUM": 3, "LOW": 0}


def test_pii_summary_schema():
    from sqldoc.pii import summarize
    keys = set(summarize([]).keys())
    assert keys == {"total", "by_risk", "by_regulation", "tables_affected"}


# --- JSON output contracts -------------------------------------------------

def test_access_check_json_schema():
    from sqldoc.access.model import AccessReport, ADUser, DatabaseAccess
    from sqldoc.access.render import build_check_json
    rep = AccessReport(user=ADUser(identifier="u", found=True))
    rep.access.append(DatabaseAccess(server="s", database="d", login="l",
                                     roles=["db_datareader"], level="read"))
    j = build_check_json(rep)
    assert j["report_type"] == "access-check"
    assert set(j.keys()) == {"report_type", "user", "matched_groups", "logins", "access", "errors"}
    assert set(j["access"][0].keys()) == {"server", "database", "login", "db_user", "via",
                                          "roles", "level", "permissions", "pii_tables"}


def test_access_script_json_schema():
    from sqldoc.access.model import AccessReport, ADUser, ParsedRequest
    from sqldoc.access.script import generate_script
    from sqldoc.access.render import build_script_json
    rep = AccessReport(user=ADUser(identifier="u", login="corp\\u", found=True))
    gs = generate_script(rep, ParsedRequest(raw="read d", database="d", level="read"),
                         "s", "d", login_override="corp\\u")
    j = build_script_json(gs)
    assert set(j.keys()) == {"report_type", "server", "database", "login", "login_type",
                             "role", "uses_windows_group", "note", "grant_sql",
                             "rollback_sql", "impact", "pii_exposed"}


def test_frameworks_control_ids():
    from sqldoc import frameworks as fw
    ctx = {"pii_findings": [], "principals": [], "access_alerts": []}
    ids = {r.framework: [c.control_id for c in r.controls]
           for r in fw.assess_all(["all"], ctx)}
    # lock the control numbers per framework
    assert ids["sox"] == ["ITGC-AC", "Section-404", "Section-302", "COBIT-DSS05"]
    assert ids["fedramp"] == ["AC-2", "AC-3", "AC-6", "AC-5", "AU-2"]
    assert ids["soc2"] == ["CC6.1", "CC6.3", "CC6.2", "CC7.2"]
    assert set(ids) == {"sox", "fedramp", "iso27001", "cmmc", "ccpa", "pipeda", "soc2"}


# --- level / role / DDL contracts ------------------------------------------

def test_roles_for_level_contract():
    from sqldoc.access.roles import roles_for_level
    assert roles_for_level("read") == ["db_datareader"]
    assert roles_for_level("write") == ["db_datareader", "db_datawriter"]
    assert roles_for_level("admin") == ["db_owner"]


def test_login_type_ddl_contract():
    from sqldoc.access import login_types as lt
    assert lt.create_login_sql("corp\\g", lt.WINDOWS) == "CREATE LOGIN [corp\\g] FROM WINDOWS;"
    assert "FROM EXTERNAL PROVIDER" in lt.create_login_sql("u@x.com", lt.AZURE_AD)
    assert "WITH PASSWORD" in lt.create_login_sql("app", lt.SQL)
    assert lt.create_user_sql("u@x.com", lt.AZURE_AD, "azuresql") == \
        "CREATE USER [u@x.com] FROM EXTERNAL PROVIDER;"


def test_permission_classification_contract():
    from sqldoc.comply import classify_permission
    assert classify_permission("SELECT") == "read"
    assert classify_permission("INSERT") == "write"
    assert classify_permission("CONTROL") == "admin"
    assert classify_permission("SELECT", "GRANT_WITH_GRANT_OPTION") == "admin"


# --- rendered report structure ---------------------------------------------

def test_doc_report_structure(tmp_path):
    """The doc HTML template's structural classes are locked (a redesign that
    drops sections must be intentional)."""
    from sqldoc.renderer import render_html
    out = str(tmp_path / "doc.html")
    render_html("DB", [_person_table()], out)
    html = open(out, encoding="utf-8").read()
    classes = set(re.findall(r'class="([^"]+)"', html))
    # a stable subset that must always be present
    required = {"container", "sidebar", "main"}
    present = {c for cls in classes for c in cls.split()}
    assert required <= present, f"missing structural classes: {required - present}"
    assert "Person" in html and "SSN" in html
