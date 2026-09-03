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
    "access", "agent", "audit", "azuredevops", "azuredevops-wiki", "backup",
    "baseline", "box", "capacity", "cms", "comply", "confluence", "dbt", "deadlocks", "doc",
    "doctor", "dropbox", "executive", "gdrive", "github-wiki", "gitlab-wiki", "ha", "health",
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
    from sqldoc.access.model import AccessReport, ADUser, DatabaseAccess, Login
    from sqldoc.access.render import build_check_json
    rep = AccessReport(user=ADUser(identifier="u", found=True))
    rep.logins.append(Login(name="l", type="WINDOWS_GROUP"))
    rep.access.append(DatabaseAccess(server="s", database="d", login="l",
                                     roles=["db_datareader"], level="read"))
    j = build_check_json(rep)
    assert j["report_type"] == "access-check"
    assert set(j.keys()) == {"report_type", "user", "matched_groups", "logins", "access", "errors"}
    # `server_permissions` and `flags` were added deliberately: both are collected
    # by the access probe and were previously exported nowhere, which made
    # server-scoped grants and escalation routes invisible to any consumer.
    assert set(j["logins"][0].keys()) == {"name", "type", "server", "is_disabled",
                                          "server_roles", "server_permissions"}
    # `principal`, `principal_type` and `has_server_login` were added deliberately
    # (v3.2.0): a Windows user or group can hold roles as a DATABASE principal with
    # no server login at all, and those grants confer real access. `login` stays as
    # it was and is left EMPTY for such a principal rather than fabricated, so
    # `principal` is the field that always names whatever actually holds the access.
    assert set(j["access"][0].keys()) == {"server", "database", "login", "db_user", "via",
                                          "roles", "level", "permissions", "pii_tables",
                                          "flags", "principal", "principal_type",
                                          "has_server_login"}


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


def test_doc_json_schema():
    """`doc --format json` is a published consumer contract, so its key sets are
    pinned here.

    This needs pinning more than the other JSON outputs, not less:
    `json_renderer.build_json` serializes the extractor dataclasses with
    `dataclasses.asdict`, so *any* field added to the model appears in the
    export automatically, with no per-field code to review. `precision` and
    `scale` reached the export exactly that way and went unnoticed.
    """
    from sqldoc.json_renderer import build_json, JSON_SCHEMA_VERSION
    from conftest import build_views, build_procs

    j = build_json("DB", [_person_table()], build_views(), build_procs())
    assert j["schema_version"] == JSON_SCHEMA_VERSION == 1, (
        "schema_version is the signal consumers key off; changing it is a "
        "breaking change and must be deliberate.")
    assert set(j.keys()) == {"schema_version", "sqldoc_version", "database",
                             "generated_at", "stats", "tables", "views",
                             "procedures"}
    assert set(j["stats"].keys()) == {"tables", "views", "procedures", "columns"}
    assert set(j["tables"][0].keys()) == {"schema", "name", "row_count", "description",
                                          "columns", "indexes", "triggers",
                                          "check_constraints", "unique_constraints"}
    # `precision` and `scale` were added so change detection can tell
    # decimal(10,2) from decimal(18,4) -- both are 9 bytes, so max_length alone
    # cannot. They are additive and default to null on adapters that do not
    # report them, so schema_version stays 1.
    assert set(j["tables"][0]["columns"][0].keys()) == {
        "name", "data_type", "max_length", "precision", "scale", "is_nullable",
        "is_primary_key", "is_foreign_key", "references_table", "references_column",
        "description", "is_computed", "computed_definition", "default_definition",
        "fk_on_delete", "fk_on_update"}
    assert set(j["views"][0].keys()) == {"schema", "name", "description", "columns",
                                         "definition"}
    assert set(j["procedures"][0].keys()) == {"schema", "name", "description",
                                              "parameters", "definition"}
    assert set(j["procedures"][0]["parameters"][0].keys()) == {
        "name", "data_type", "max_length", "is_output"}


def test_doc_json_index_and_constraint_schema():
    """Index/trigger/constraint shapes ride the same asdict export."""
    from sqldoc.json_renderer import build_json
    from conftest import build_tables

    t = build_json("DB", build_tables())["tables"][0]
    assert set(t["indexes"][0].keys()) == {"name", "type_desc", "is_unique",
                                           "is_primary_key", "key_columns",
                                           "included_columns"}
    assert set(t["triggers"][0].keys()) == {"name", "is_instead_of", "is_disabled",
                                            "events", "definition"}
    assert set(t["check_constraints"][0].keys()) == {"name", "definition", "column"}
    assert set(t["unique_constraints"][0].keys()) == {"name", "columns"}


def test_linked_server_summary_schema():
    """`summarize_linked` is the linked-server summary that reaches both the
    intel HTML report and the CLI, so its key set is pinned.

    `not_tested` was added deliberately: `probe_connectivity` is three-state, so
    `reachable + unreachable` no longer sums to the total, and a server whose
    probe could not RUN (permission denied, RPC off) would otherwise be absent
    from every bucket -- reinstating in the summary the exact silence the
    three-state classification exists to remove. Additive; no key was removed or
    renamed, so no consumer breaks.
    """
    from sqldoc.intel import LinkedServer, LinkedServerReport, summarize_linked

    rep = LinkedServerReport(local_server="LOCAL", linked_servers=[
        LinkedServer(name="UP", reachable=True),
        LinkedServer(name="DOWN", reachable=False),
        LinkedServer(name="DENIED", reachable=None),
    ])
    s = summarize_linked(rep)
    assert set(s.keys()) == {"linked_servers", "reachable", "unreachable",
                             "not_tested", "rpc_out_enabled", "data_access_enabled"}
    assert (s["linked_servers"], s["reachable"], s["unreachable"], s["not_tested"])         == (3, 1, 1, 1)
    # the three states must partition the set -- nothing may go uncounted
    assert s["reachable"] + s["unreachable"] + s["not_tested"] == s["linked_servers"]


def test_agent_event_type_inventory():
    """The agent's subscribable event types are a config contract: a user's
    `notify.on:` list is validated against it, so removing or renaming one
    breaks existing configs at parse time.

    `ha_posture` and `escalation_failed` were added deliberately -- both were
    being recorded in the store while being impossible to subscribe to, which
    made each a fix that reported into a place nobody is watching.
    """
    from sqldoc.agent.config import EVENT_TYPES

    assert set(EVENT_TYPES) == {
        "schema_change", "new_pii", "health_degradation", "job_failure",
        "disk_low", "errorlog_critical", "linked_server_down", "backup_stale",
        "replica_lag", "tempdb_version_store", "nl_alert", "doc_updated",
        "ha_posture", "escalation_failed",
        "cms_server_added", "cms_server_removed", "cms_server_unreachable"}
    assert len(EVENT_TYPES) == len(set(EVENT_TYPES)), "duplicate event type"


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
