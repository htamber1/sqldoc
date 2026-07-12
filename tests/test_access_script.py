"""Access script generation + rollback + impact analysis + render."""
from sqldoc.access.script import generate_script, pick_login
from sqldoc.access.model import AccessReport, ADUser, DatabaseAccess, Login, ParsedRequest
from sqldoc.access.render import build_script_json, render_script_html
from sqldoc.extractor import Table, Column


def _user():
    return ADUser(identifier="jsmith", display_name="Jane Smith", sam_account_name="jsmith",
                  login="CORP\\jsmith", groups=["Sales Team"], found=True)


def _tables():
    return [
        Table(schema="Sales", name="Customer", row_count=10, columns=[
            Column("Id", "int", 4, False, True, False, None, None),
            Column("Email", "varchar", 100, True, False, False, None, None)]),
        Table(schema="Sales", name="Orders", row_count=5, columns=[
            Column("Id", "int", 4, False, True, False, None, None)]),
    ]


def _pii():
    from sqldoc.pii import scan_tables
    return scan_tables(_tables())


# --- pick_login ------------------------------------------------------------

def test_pick_login_prefers_existing_group():
    r = AccessReport(user=_user())
    r.access.append(DatabaseAccess(server="prod", database="Sales", login="CORP\\Sales Team",
                                   via="group CORP\\Sales Team", roles=["db_datareader"], level="read"))
    login, is_group, note = pick_login(r, ParsedRequest(raw="", database="Sales", level="write"))
    assert login == "CORP\\Sales Team" and is_group


def test_pick_login_uses_ad_group_login():
    r = AccessReport(user=_user())
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    login, is_group, _ = pick_login(r, ParsedRequest(raw="", database="Sales", level="read"))
    assert login == "CORP\\Sales Team" and is_group


def test_pick_login_falls_back_to_individual():
    r = AccessReport(user=_user())
    login, is_group, note = pick_login(r, ParsedRequest(raw="", database="Sales", level="read"))
    assert login == "CORP\\jsmith" and not is_group
    assert "individual login" in note


def test_pick_login_override():
    r = AccessReport(user=_user())
    login, is_group, note = pick_login(r, ParsedRequest(raw="", database="Sales"),
                                       override="CORP\\Custom Group")
    assert login == "CORP\\Custom Group" and is_group and "caller" in note


# --- generate_script -------------------------------------------------------

def test_generate_write_script_new_access():
    r = AccessReport(user=_user())
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    parsed = ParsedRequest(raw="write access to Sales", database="Sales", level="write")
    gs = generate_script(r, parsed, "prod", "Sales", tables=_tables(), pii_findings=_pii())
    assert gs.uses_windows_group
    # best-practice statements present
    assert "CREATE LOGIN [CORP\\Sales Team] FROM WINDOWS" in gs.grant_sql
    assert "IF NOT EXISTS" in gs.grant_sql
    assert "CREATE USER" in gs.grant_sql
    assert "ALTER ROLE [db_datareader] ADD MEMBER" in gs.grant_sql
    assert "ALTER ROLE [db_datawriter] ADD MEMBER" in gs.grant_sql
    # rollback drops exactly those
    assert "DROP MEMBER [CORP\\Sales Team]" in gs.rollback_sql
    assert "db_datawriter" in gs.rollback_sql and "db_datareader" in gs.rollback_sql
    # impact + PII
    assert "Sales.Customer" in gs.impact and "Sales.Orders" in gs.impact
    assert any(t == "Customer" for (_s, t, _r, _g) in gs.pii_exposed)


def test_generate_individual_login_uses_password_placeholder():
    r = AccessReport(user=ADUser(identifier="svc_app", sam_account_name="svc_app",
                                 login="svcapp", groups=[], found=True))
    parsed = ParsedRequest(raw="read Sales", database="Sales", level="read")
    gs = generate_script(r, parsed, "prod", "Sales", tables=_tables(), pii_findings=_pii(),
                         login_override="svcapp")
    assert not gs.uses_windows_group
    assert "WITH PASSWORD" in gs.grant_sql       # SQL login path


def test_generate_already_has_access_is_noop():
    r = AccessReport(user=_user())
    r.access.append(DatabaseAccess(server="prod", database="Sales", login="CORP\\Sales Team",
                                   via="group", roles=["db_datareader", "db_datawriter"], level="write"))
    parsed = ParsedRequest(raw="write Sales", database="Sales", level="write")
    gs = generate_script(r, parsed, "prod", "Sales", tables=_tables())
    assert "No changes" in gs.note
    assert "No changes required" in gs.grant_sql
    assert "Nothing to roll back" in gs.rollback_sql


def test_identifier_quoting_escapes_bracket():
    from sqldoc.access.script import _q
    assert _q("weird]name") == "[weird]]name]"


# --- render ----------------------------------------------------------------

def test_build_script_json():
    r = AccessReport(user=_user())
    parsed = ParsedRequest(raw="read Sales", database="Sales", level="read")
    gs = generate_script(r, parsed, "prod", "Sales", tables=_tables(), pii_findings=_pii(),
                         login_override="CORP\\Sales Team")
    j = build_script_json(gs)
    assert j["report_type"] == "access-script"
    assert "grant_sql" in j and "rollback_sql" in j
    assert j["pii_exposed"]


def test_render_script_html_offline(tmp_path):
    from sqldoc.offline import verify_file
    r = AccessReport(user=_user())
    parsed = ParsedRequest(raw="write Sales", database="Sales", level="write")
    gs = generate_script(r, parsed, "prod", "Sales", tables=_tables(), pii_findings=_pii(),
                         login_override="CORP\\Sales Team")
    out = tmp_path / "script.html"
    render_script_html(gs, str(out))
    text = out.read_text(encoding="utf-8")
    assert "Grant SQL" in text and "Rollback SQL" in text and "Customer" in text
    assert verify_file(str(out)) == []


# --- CLI -------------------------------------------------------------------

def test_cli_access_script(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.access import checker
    cfg = {"access": {"ad": {"type": "ldap", "server": "x", "base_dn": "y"},
                      "servers": [{"name": "prod", "connection_string": "c",
                                   "dialect": "sqlserver", "databases": ["Sales"]}]}}
    r = AccessReport(user=_user())
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    monkeypatch.setattr(checker, "check_access", lambda c, ident, **k: r)
    monkeypatch.setattr(cli, "_access_tables_for",
                        lambda cfg, db: (_tables(), _pii(), "prod", "sqlserver", None))
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    sql_out = tmp_path / "grant.sql"
    res = CliRunner().invoke(cli.cli, ["access", "script", "--config", str(p), "--user", "jsmith",
                                       "--database", "Sales", "--level", "write", "--no-ai",
                                       "--output", str(tmp_path / "s.html"),
                                       "--sql-out", str(sql_out)])
    assert res.exit_code == 0, res.output
    assert "ALTER ROLE" in res.output
    assert sql_out.exists() and (tmp_path / "grant.rollback.sql").exists()
