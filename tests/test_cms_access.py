"""Estate-wide access audit: aggregation, orphan detection, render, CLI."""
import json

import pytest

from sqldoc.access import cms_review
from sqldoc.access.cms_review import (aggregate, collect_estate_access,
                                      build_estate_access_json, render_estate_access_html)
from sqldoc.cms_bulk import ServerResult
from sqldoc.cms import CmsInventory, CmsServer
from sqldoc.access.model import ADUser


def _res(server, logins, ok=True, error=""):
    return ServerResult(server=server, host=server, ok=ok, error=error,
                        summary={"logins": logins} if ok else {})


def _login(name, roles=(), type="WINDOWS_LOGIN", disabled=False):
    return {"name": name, "type": type, "is_disabled": disabled, "server_roles": list(roles)}


# --- aggregation -----------------------------------------------------------

def test_elevated_on_multiple_servers():
    results = [
        _res("s1", [_login("CORP\\dba", ["sysadmin"]), _login("CORP\\app")]),
        _res("s2", [_login("CORP\\dba", ["sysadmin"]), _login("CORP\\app")]),
        _res("s3", [_login("CORP\\app")]),
    ]
    rep = aggregate(results)
    elevated = [x for x in rep.elevated_multi if x["principal"] == "CORP\\dba"]
    assert elevated and set(elevated[0]["servers"]) == {"s1", "s2"}
    assert elevated[0]["roles"] == ["sysadmin"]


def test_coverage_gaps():
    results = [
        _res("s1", [_login("CORP\\a"), _login("CORP\\b")]),
        _res("s2", [_login("CORP\\a")]),               # b missing from s2
    ]
    rep = aggregate(results)
    gaps = {g["principal"]: g for g in rep.coverage_gaps}
    assert "CORP\\b" in gaps
    assert gaps["CORP\\b"]["present_on"] == ["s1"] and gaps["CORP\\b"]["missing_from"] == ["s2"]
    assert "CORP\\a" not in gaps                        # present everywhere


def test_system_principals_skipped():
    results = [
        _res("s1", [_login("##MS_Cert##", ["sysadmin"]), _login("NT AUTHORITY\\SYSTEM", ["sysadmin"])]),
        _res("s2", [_login("##MS_Cert##", ["sysadmin"])]),
    ]
    rep = aggregate(results)
    assert not rep.elevated_multi and not rep.coverage_gaps    # system principals ignored


def test_orphaned_with_ad_source():
    class Source:
        def get_user(self, part):
            return ADUser(identifier=part, found=(part.lower() != "ghost"))
    results = [_res("s1", [_login("CORP\\ghost"), _login("CORP\\real")])]
    rep = aggregate(results, source=Source())
    assert [o["principal"] for o in rep.orphaned] == ["CORP\\ghost"]


def test_failed_servers_recorded():
    results = [_res("s1", [_login("CORP\\a")]), _res("s2", [], ok=False, error="timeout")]
    rep = aggregate(results)
    assert rep.servers == ["s1"] and rep.failed == [("s2", "timeout")]


# --- collect ---------------------------------------------------------------

def test_collect_estate_access(monkeypatch):
    inv = CmsInventory(cms_server="CMS", groups=[],
                       servers=[CmsServer(name="s1", server_name="s1"),
                                CmsServer(name="s2", server_name="s2")])
    monkeypatch.setattr(cms_review, "_login_worker",
                        lambda server, opts: {"logins": [_login("CORP\\dba", ["sysadmin"])]})
    rep = collect_estate_access(inv, {}, max_workers=2)
    assert len(rep.servers) == 2 and rep.elevated_multi


# --- render + json ---------------------------------------------------------

def _report():
    return aggregate([
        _res("s1", [_login("CORP\\dba", ["sysadmin"]), _login("CORP\\b")]),
        _res("s2", [_login("CORP\\dba", ["sysadmin"])]),
    ])


def test_build_json():
    j = build_estate_access_json(_report())
    assert j["report_type"] == "cms-access-review" and j["servers_audited"] == 2
    assert j["elevated_on_multiple"] and j["coverage_gaps"]


def test_render_offline(tmp_path):
    from sqldoc.offline import verify_file
    out = tmp_path / "audit.html"
    render_estate_access_html(_report(), str(out))
    text = out.read_text(encoding="utf-8")
    assert "Estate-wide access review" in text and "CORP\\dba" in text
    assert "Elevated on multiple servers" in text and "Coverage gaps" in text
    assert verify_file(str(out)) == []


# --- CLI -------------------------------------------------------------------

def test_cli_access_review_cms(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    inv = CmsInventory(cms_server="CMS", groups=[],
                       servers=[CmsServer(name="s1", server_name="s1"),
                                CmsServer(name="s2", server_name="s2")])
    monkeypatch.setattr(cli, "_load_cms_inventory", lambda cfg: inv)
    monkeypatch.setattr(cms_review, "_login_worker",
                        lambda server, opts: {"logins": [_login("CORP\\dba", ["sysadmin"])]})
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"cms": {"server": "CMS"},
                                 "access": {"ad": {"type": "native"}}}), encoding="utf-8")
    out, js = tmp_path / "a.html", tmp_path / "a.json"
    res = CliRunner().invoke(cli.cli, ["access", "review", "--cms", "--config", str(p),
                                       "--output", str(out), "--json", str(js)])
    assert res.exit_code == 0, res.output
    assert "elevated on multiple servers" in res.output
    data = json.loads(js.read_text())
    assert data["report_type"] == "cms-access-review" and data["servers_audited"] == 2
