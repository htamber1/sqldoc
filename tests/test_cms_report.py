"""CMS inventory report: probe, collect, render, CLI. Fake cursor/workers."""
import json

import pytest

from sqldoc import cms_report
from sqldoc.cms_report import probe_server, collect_report, build_report_json
from sqldoc.cms_renderer import render_inventory_report_html
from sqldoc.cms_bulk import ServerResult
from sqldoc.cms import CmsInventory, CmsGroup, CmsServer


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def execute(self, sql, *a):
        pass

    def fetchall(self):
        return [self.row]


def _inv():
    return CmsInventory(cms_server="CMS",
                        groups=[CmsGroup(id=2, name="Prod", parent_id=1, path="Prod")],
                        servers=[CmsServer(name="p1", server_name="p1.corp", group_id=2, group_path="Prod"),
                                 CmsServer(name="p2", server_name="p2.corp", group_id=2, group_path="Prod")])


# --- probe -----------------------------------------------------------------

def test_probe_server():
    row = {"version": "16.0.1000.6", "edition": "Developer Edition", "product_level": "RTM",
           "db_count": 12, "uptime_hours": 50}
    out = probe_server(FakeCursor(row))
    assert out["version"] == "16.0.1000.6" and out["edition"] == "Developer Edition"
    assert out["db_count"] == 12 and out["uptime_hours"] == 50


# --- collect ---------------------------------------------------------------

def test_collect_report(monkeypatch):
    monkeypatch.setattr(cms_report, "_worker",
                        lambda server, opts: {"version": "16.0", "edition": "Std",
                                              "product_level": "RTM", "db_count": 5,
                                              "uptime_hours": 24})

    class Store:
        def last_run(self, name):
            return {"finished_at": "2026-07-12T00:00:00"} if name == "p1" else None
    results = collect_report(_inv(), {}, store=Store())
    assert len(results) == 2 and all(r.ok for r in results)
    p1 = next(r for r in results if r.server == "p1")
    assert p1.summary["last_run"] == "2026-07-12T00:00:00"
    p2 = next(r for r in results if r.server == "p2")
    assert p2.summary["last_run"] is None


def test_collect_report_failure_isolated(monkeypatch):
    def worker(server, opts):
        if server.name == "p2":
            raise RuntimeError("timeout")
        return {"version": "16", "edition": "X", "product_level": "RTM", "db_count": 1, "uptime_hours": 1}
    monkeypatch.setattr(cms_report, "_worker", worker)
    results = collect_report(_inv(), {})
    assert sum(1 for r in results if r.ok) == 1
    assert any(not r.ok and "timeout" in r.error for r in results)


# --- render + json ---------------------------------------------------------

def _results():
    return [
        ServerResult(server="p1", host="p1.corp", group="Prod", ok=True,
                     summary={"version": "16.0", "edition": "Developer", "product_level": "RTM",
                              "db_count": 8, "uptime_hours": 50, "last_run": "2026-07-12"}),
        ServerResult(server="p2", host="p2.corp", group="Prod", ok=False, error="login failed"),
    ]


def test_build_report_json():
    j = build_report_json(_inv(), _results())
    assert j["report_type"] == "cms-inventory-report" and j["server_count"] == 2
    assert j["reachable"] == 1
    assert j["servers"][0]["edition"] == "Developer"


def test_render_inventory_report_offline(tmp_path):
    from sqldoc.offline import verify_file
    out = tmp_path / "report.html"
    render_inventory_report_html(_inv(), _results(), str(out))
    text = out.read_text(encoding="utf-8")
    assert "Developer" in text and "2d 2h" in text        # uptime formatted (50h)
    assert "unreachable" in text and "login failed" in text
    assert "Last sqldoc run" in text
    assert verify_file(str(out)) == []


# --- CLI -------------------------------------------------------------------

def test_cli_cms_report(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    monkeypatch.setattr(cli, "_load_cms_inventory", lambda cfg: _inv())
    monkeypatch.setattr(cms_report, "_worker",
                        lambda server, opts: {"version": "16.0", "edition": "Developer",
                                              "product_level": "RTM", "db_count": 3, "uptime_hours": 10})
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"cms": {"server": "CMS"}}), encoding="utf-8")
    out, js = tmp_path / "r.html", tmp_path / "r.json"
    res = CliRunner().invoke(cli.cli, ["cms", "report", "--config", str(p),
                                       "--output", str(out), "--json", str(js)])
    assert res.exit_code == 0, res.output
    assert "2 reachable" in res.output and "Developer" in res.output
    data = json.loads(js.read_text())
    assert data["report_type"] == "cms-inventory-report" and data["reachable"] == 2
