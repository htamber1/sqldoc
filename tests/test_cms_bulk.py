"""CMS bulk-run orchestration + aggregated rendering. Workers are mocked, so no
live SQL Server; failure isolation and aggregation are exercised."""
import json

import pytest

from sqldoc import cms_bulk
from sqldoc.cms import CmsInventory, CmsGroup, CmsServer
from sqldoc.cms_bulk import run_against_servers, run_bulk, ServerResult
from sqldoc.cms_renderer import render_bulk_html, build_bulk_json


def _inv():
    groups = [CmsGroup(id=2, name="Production", parent_id=1, path="Production"),
              CmsGroup(id=3, name="Dev", parent_id=1, path="Dev")]
    servers = [CmsServer(name="p1", server_name="p1.corp", group_id=2, group_path="Production"),
               CmsServer(name="p2", server_name="p2.corp", group_id=2, group_path="Production"),
               CmsServer(name="d1", server_name="d1.corp", group_id=3, group_path="Dev")]
    return CmsInventory(cms_server="CMS", groups=groups, servers=servers)


# --- runner ----------------------------------------------------------------

def test_run_against_servers_parallel():
    inv = _inv()

    def worker(server, opts):
        return {"host": server.server_name, "value": 1}
    results = run_against_servers(inv.servers, worker, {}, max_workers=4)
    assert len(results) == 3 and all(r.ok for r in results)
    assert {r.server for r in results} == {"p1", "p2", "d1"}


def test_failure_isolated():
    inv = _inv()

    def worker(server, opts):
        if server.name == "p2":
            raise RuntimeError("unreachable")
        return {"ok": 1}
    results = run_against_servers(inv.servers, worker, {})
    failed = [r for r in results if not r.ok]
    assert len(failed) == 1 and failed[0].server == "p2"
    assert "unreachable" in failed[0].error
    assert sum(1 for r in results if r.ok) == 2      # others still ran


def test_run_bulk_dispatches_worker(monkeypatch):
    inv = _inv()
    seen = []
    monkeypatch.setitem(cms_bulk.WORKERS, "health",
                        lambda server, opts: seen.append(server.name) or {"issues": 3})
    results = run_bulk(inv, "health", {}, max_workers=8)
    assert len(results) == 3 and set(seen) == {"p1", "p2", "d1"}
    assert all(r.summary["issues"] == 3 for r in results)


def test_run_bulk_group_filter(monkeypatch):
    inv = _inv()
    monkeypatch.setitem(cms_bulk.WORKERS, "scan", lambda server, opts: {"total": 1})
    results = run_bulk(inv, "scan", {}, group="Production")
    assert {r.server for r in results} == {"p1", "p2"}


def test_run_bulk_unknown_command():
    with pytest.raises(ValueError):
        run_bulk(_inv(), "nonexistent", {})


def test_all_commands_have_workers():
    assert set(cms_bulk.CMS_COMMANDS) == {
        "doc", "scan", "health", "quality", "intel", "comply", "server", "secure", "backup"}


# --- rendering -------------------------------------------------------------

def _results():
    return [
        ServerResult(server="p1", host="p1.corp", group="Production", ok=True,
                     summary={"HIGH": 2, "MEDIUM": 3, "total": 5}),
        ServerResult(server="p2", host="p2.corp", group="Production", ok=True,
                     summary={"HIGH": 1, "MEDIUM": 0, "total": 1}),
        ServerResult(server="d1", host="d1.corp", group="Dev", ok=False,
                     error="OperationalError: timeout"),
    ]


def test_build_bulk_json():
    j = build_bulk_json("scan", _results())
    assert j["report_type"] == "cms-bulk-scan" and j["server_count"] == 3
    assert j["ok"] == 2 and j["failed"] == 1
    assert j["results"][0]["summary"]["total"] == 5


def test_render_bulk_html_offline(tmp_path):
    from sqldoc.offline import verify_file
    out = tmp_path / "bulk.html"
    render_bulk_html("scan", _results(), str(out), group="Production")
    text = out.read_text(encoding="utf-8")
    assert "p1.corp" in text and "failed" in text and "timeout" in text
    assert "Estate total" in text            # totals row for numeric columns
    assert "6" in text                       # HIGH total (2+1) or total column sum
    assert verify_file(str(out)) == []


# --- CLI wiring ------------------------------------------------------------

def test_cli_health_cms(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli, cms as cms_mod
    inv = _inv()
    monkeypatch.setattr(cli, "_load_cms_inventory", lambda cfg: inv)
    monkeypatch.setitem(cms_bulk.WORKERS, "health",
                        lambda server, opts: {"issues": 2, "slow_queries": 1})
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"cms": {"server": "CMS", "windows_auth": True}}), encoding="utf-8")
    out, js = tmp_path / "estate.html", tmp_path / "estate.json"
    res = CliRunner().invoke(cli.cli, ["health", "--cms", "--config", str(p),
                                       "--output", str(out), "--json", str(js)])
    assert res.exit_code == 0, res.output
    assert "3 server(s)" in res.output and "3 ok" in res.output
    data = json.loads(js.read_text())
    assert data["report_type"] == "cms-bulk-health" and data["server_count"] == 3


def test_cli_scan_cms_group(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    monkeypatch.setattr(cli, "_load_cms_inventory", lambda cfg: _inv())
    monkeypatch.setitem(cms_bulk.WORKERS, "scan", lambda server, opts: {"total": 0})
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"cms": {"server": "CMS"}}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["scan", "--cms", "--group", "Production",
                                       "--config", str(p), "--output", str(tmp_path / "s.html")])
    assert res.exit_code == 0, res.output
    assert "2 server(s) in group 'Production'" in res.output
