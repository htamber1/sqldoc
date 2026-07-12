"""Access request parsing + gap analysis + render."""
import pytest

from sqldoc.access import parse as parse_mod
from sqldoc.access.parse import parse_request, heuristic_parse
from sqldoc.access.gap import analyze_gap
from sqldoc.access.model import AccessReport, ADUser, DatabaseAccess, ParsedRequest
from sqldoc.access.render import build_request_json, render_request_html


# --- heuristic parse -------------------------------------------------------

@pytest.mark.parametrize("text,db,schema,level", [
    ("read access to the Sales database", "sales", "", "read"),
    ("write access to the HR schema", "", "hr", "write"),
    ("needs to modify data in Warehouse", "warehouse", "", "write"),
    ("full control / owner of the Finance database", "finance", "", "admin"),
    ("give them select on Sales", "sales", "", "read"),
])
def test_heuristic_parse(text, db, schema, level):
    p = heuristic_parse(text, known_databases=["Sales", "HR", "Warehouse", "Finance"])
    if db:
        assert p.database.lower() == db
    if schema:
        assert p.schema.lower() == schema
    assert p.level == level


def test_parse_no_ai_uses_heuristic():
    p = parse_request("read access to Sales", known_databases=["Sales"], no_ai=True)
    assert p.database == "Sales" and p.level == "read" and "heuristic" in p.note


def test_parse_ai_json(monkeypatch):
    import sqldoc.ai as real_ai
    monkeypatch.setattr(real_ai, "dispatch",
                        lambda *a, **k: 'Sure: {"database":"HR","schema":"Payroll","level":"write","objects":[]}')
    p = parse_request("let jsmith update payroll in HR", known_databases=["HR"])
    assert p.database == "HR" and p.schema == "Payroll" and p.level == "write"
    assert p.confidence >= 0.8


def test_parse_ai_failure_falls_back(monkeypatch):
    import sqldoc.ai as real_ai
    def boom(*a, **k):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(real_ai, "dispatch", boom)
    p = parse_request("read access to Sales", known_databases=["Sales"])
    assert p.database == "Sales" and "fallback" in p.note


def test_parse_ai_backfills_database(monkeypatch):
    import sqldoc.ai as real_ai
    monkeypatch.setattr(real_ai, "dispatch",
                        lambda *a, **k: '{"database":"","schema":"","level":"read"}')
    p = parse_request("read access to the Sales database", known_databases=["Sales"])
    assert p.database == "Sales"     # heuristic backfilled


# --- gap analysis ----------------------------------------------------------

def _report(level="none", roles=None, database="Sales"):
    u = ADUser(identifier="jsmith", display_name="Jane Smith", found=True)
    r = AccessReport(user=u)
    if level != "none":
        r.access.append(DatabaseAccess(server="prod", database=database, login="CORP\\Sales",
                                       roles=roles or [], level=level))
    return r


def test_gap_already():
    g = analyze_gap(ParsedRequest(raw="read Sales", database="Sales", level="read"),
                    _report("read", ["db_datareader"]))
    assert g.verdict == "ALREADY" and not g.missing
    assert "already has read" in g.explanation


def test_gap_none():
    g = analyze_gap(ParsedRequest(raw="read Sales", database="Sales", level="read"),
                    _report("none"))
    assert g.verdict == "NONE"
    assert any("db_datareader" in m for m in g.missing)


def test_gap_partial_read_needs_write():
    g = analyze_gap(ParsedRequest(raw="write Sales", database="Sales", level="write"),
                    _report("read", ["db_datareader"]))
    assert g.verdict == "PARTIAL" and g.have_level == "read" and g.needs_level == "write"
    assert any("db_datawriter" in m for m in g.missing)


def test_gap_admin_satisfies_write():
    g = analyze_gap(ParsedRequest(raw="write Sales", database="Sales", level="write"),
                    _report("admin", ["db_owner"]))
    assert g.verdict == "ALREADY"


# --- render ----------------------------------------------------------------

def test_build_request_json():
    report = _report("read", ["db_datareader"])
    parsed = ParsedRequest(raw="read Sales", database="Sales", level="read", note="AI parse")
    gap = analyze_gap(parsed, report)
    j = build_request_json(report, parsed, gap)
    assert j["report_type"] == "access-request" and j["verdict"] == "ALREADY"


def test_render_request_html_offline(tmp_path):
    from sqldoc.offline import verify_file
    report = _report("read", ["db_datareader"])
    parsed = ParsedRequest(raw="write access to Sales", database="Sales", level="write", note="AI parse")
    gap = analyze_gap(parsed, report)
    out = tmp_path / "req.html"
    render_request_html(report, parsed, gap, str(out))
    text = out.read_text(encoding="utf-8")
    assert "PARTIAL" in text and "Sales" in text
    assert verify_file(str(out)) == []


# --- CLI -------------------------------------------------------------------

def test_cli_access_request(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.access import checker
    cfg = {"access": {"ad": {"type": "ldap", "server": "x", "base_dn": "y"},
                      "servers": [{"name": "prod", "connection_string": "c",
                                   "dialect": "sqlserver", "databases": ["Sales"]}]}}
    monkeypatch.setattr(checker, "check_access",
                        lambda c, ident, **k: _report("read", ["db_datareader"]))
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["access", "request", "--config", str(p), "--user", "jsmith",
                                       "--request", "write access to the Sales database", "--no-ai",
                                       "--output", str(tmp_path / "r.html")])
    assert res.exit_code == 0, res.output
    assert "PARTIAL" in res.output
