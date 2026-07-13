"""CMS executive estate dashboard: aggregation + rendering + CLI wiring."""
import json

import pytest

from sqldoc import cms_executive
from sqldoc.cms_executive import aggregate, build_estate_json, EstateSummary
from sqldoc.cms_executive_renderer import render_estate_html
from sqldoc.cms_bulk import ServerResult
from sqldoc.cms import CmsInventory, CmsGroup, CmsServer


def _inv():
    return CmsInventory(cms_server="CMS",
                        groups=[CmsGroup(id=2, name="Prod", parent_id=1, path="Prod")],
                        servers=[CmsServer(name="p1", server_name="p1", group_id=2, group_path="Prod"),
                                 CmsServer(name="p2", server_name="p2", group_id=2, group_path="Prod")])


def _server_summary(name, overall, pii, backup, sec, health, findings, risks):
    return {"server": name, "host": name, "group": "Prod", "overall": overall,
            "pii_safety": pii, "backup_pct": backup, "security": sec, "security_grade": "B",
            "health": health, "pii_findings": findings, "tables": 10, "db_count": 3,
            "top_risks": risks}


def _results():
    return [
        ServerResult(server="p1", host="p1", group="Prod", ok=True,
                     summary=_server_summary("p1", 80, 90, 100, 70, 85, 5,
                                             [{"severity": "High", "title": "Blank password"}])),
        ServerResult(server="p2", host="p2", group="Prod", ok=True,
                     summary=_server_summary("p2", 60, 50, 50, 60, 80, 12,
                                             [{"severity": "Critical", "title": "SA enabled"}])),
        ServerResult(server="p3", host="p3", group="Prod", ok=False, error="timeout"),
    ]


# --- aggregation -----------------------------------------------------------

def test_aggregate_scores_and_counts():
    est = aggregate(_results())
    assert est.server_count == 2                 # only reachable servers
    assert est.database_count == 6               # 3 + 3
    assert est.pii_total == 17                   # 5 + 12
    assert est.overall == 70                     # (80+60)/2
    assert est.backup == 75                      # (100+50)/2


def test_aggregate_top_risks_sorted_and_tagged():
    est = aggregate(_results())
    assert est.top_risks[0]["severity"] == "Critical"       # sorted, critical first
    assert est.top_risks[0]["server"] == "p2"               # tagged with server
    assert len(est.top_risks) <= 10


def test_aggregate_all_failed():
    failed = [ServerResult(server="x", host="x", ok=False, error="down")]
    est = aggregate(failed)
    assert est.server_count == 0 and est.overall is None and est.top_risks == []


# --- collect_estate worker -------------------------------------------------

def test_collect_estate(monkeypatch):
    inv = _inv()
    monkeypatch.setattr(cms_executive, "_exec_worker",
                        lambda server, opts: _server_summary(server.name, 75, 80, 90, 70, 85, 3, []))
    est = cms_executive.collect_estate(inv, {}, max_workers=4)
    assert est.server_count == 2 and est.overall == 75


# --- json + render ---------------------------------------------------------

def test_build_estate_json():
    j = build_estate_json(aggregate(_results()))
    assert j["report_type"] == "cms-executive"
    assert j["server_count"] == 2 and j["scores"]["overall"] == 70
    assert len(j["servers"]) == 3               # includes the failed one


def test_render_estate_html_offline(tmp_path):
    from sqldoc.offline import verify_file
    out = tmp_path / "estate.html"
    render_estate_html(aggregate(_results()), str(out), cms_server="CMS")
    text = out.read_text(encoding="utf-8")
    assert "executive summary" in text.lower() and "SA enabled" in text
    assert "unreachable" in text and "p3" in text        # failed server marked
    assert "Top risks across the estate" in text
    assert verify_file(str(out)) == []


# --- CLI -------------------------------------------------------------------

def test_cli_executive_cms(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    monkeypatch.setattr(cli, "_load_cms_inventory", lambda cfg: _inv())
    monkeypatch.setattr(cms_executive, "_exec_worker",
                        lambda server, opts: _server_summary(server.name, 70, 80, 90, 60, 85, 4,
                                                             [{"severity": "High", "title": "x"}]))
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"cms": {"server": "CMS"}}), encoding="utf-8")
    out, js = tmp_path / "e.html", tmp_path / "e.json"
    res = CliRunner().invoke(cli.cli, ["executive", "--cms", "--config", str(p),
                                       "--output", str(out), "--json", str(js)])
    assert res.exit_code == 0, res.output
    assert "Estate overall" in res.output and "2 server(s)" in res.output
    data = json.loads(js.read_text())
    assert data["report_type"] == "cms-executive" and data["server_count"] == 2
