"""Linked-server network mapping in `sqldoc intel`."""
import json

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.intel import (discover_linked_servers, get_linked_logins, probe_connectivity,
                          collect_linked_servers, summarize_linked, collect_intel)
from sqldoc.intel_renderer import build_intel_json, render_intel_html
from sqldoc.adapters.sqlserver import SqlServerAdapter
from conftest import FakeConnection, FakeAdapter


def test_discover_linked_servers(fake_linked_rows):
    servers = discover_linked_servers(FakeConnection(fake_linked_rows).cursor())
    assert [s.name for s in servers] == ["REPORTING01", "LEGACY_ORA"]
    rpt = servers[0]
    assert rpt.product == "SQL Server" and rpt.is_rpc_out and rpt.is_data_access
    ora = servers[1]
    assert ora.product == "Oracle" and not ora.is_rpc_out


def test_get_linked_logins(fake_linked_rows):
    logins = get_linked_logins(FakeConnection(fake_linked_rows).cursor())
    assert logins["REPORTING01"][0].local_login == "(all logins)"
    assert logins["LEGACY_ORA"][0].remote_login == "system"


def test_connectivity_ok(fake_linked_rows):
    ok, msg = probe_connectivity(FakeConnection(fake_linked_rows).cursor(), "REPORTING01")
    assert ok and msg == "OK"


def test_connectivity_failure():
    class Boom:
        def execute(self, *a, **k):
            raise ConnectionError("cannot reach host")
    ok, msg = probe_connectivity(Boom(), "DEAD")
    assert not ok and "cannot reach host" in msg


def test_collect_linked_servers(fake_linked_rows):
    report = collect_linked_servers(FakeAdapter(FakeConnection(fake_linked_rows)))
    assert report.local_server == "PRODSQL01"
    assert len(report.linked_servers) == 2
    rpt = report.linked_servers[0]
    assert rpt.reachable is True                     # sp_testlinkedserver "succeeds"
    assert rpt.logins and rpt.logins[0].remote_login == "rpt_reader"
    assert not report.traversed
    # not traversed -> no remote version probed
    assert rpt.remote_version == ""

    s = summarize_linked(report)
    assert s["linked_servers"] == 2 and s["reachable"] == 2
    assert s["data_access_enabled"] == 2 and s["rpc_out_enabled"] == 1


def test_collect_linked_servers_traverse(fake_linked_rows):
    report = collect_linked_servers(FakeAdapter(FakeConnection(fake_linked_rows)), traverse=True)
    assert report.traversed
    rpt = report.linked_servers[0]
    assert rpt.remote_version == "15.0.4123.1"
    assert "Enterprise" in rpt.remote_edition


def test_intel_json_and_render_with_linked(fake_linked_rows, tmp_path):
    report = collect_intel("DB", [])
    report.linked_servers = collect_linked_servers(FakeAdapter(FakeConnection(fake_linked_rows)),
                                                   traverse=True)
    data = build_intel_json("DB", report)
    assert data["linked_servers"]["local_server"] == "PRODSQL01"
    assert data["linked_servers"]["summary"]["linked_servers"] == 2
    assert data["summary"]["linked_servers"] == 2

    out = tmp_path / "intel.html"
    render_intel_html("DB", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Linked servers" in h and "REPORTING01" in h and "LEGACY_ORA" in h
    assert "<svg" in h and "PRODSQL01" in h          # topology diagram + center node
    assert "reachable" in h


def test_intel_cli_linked_servers(monkeypatch, fake_linked_rows, tmp_path):
    monkeypatch.setattr(cli, "extract_metadata", lambda adapter: [])
    monkeypatch.setattr(cli, "extract_views", lambda adapter: [])
    monkeypatch.setattr(cli, "extract_procedures", lambda adapter: [])
    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_linked_rows)))
    out = tmp_path / "intel.html"
    jout = tmp_path / "intel.json"
    res = CliRunner().invoke(cli.cli, [
        "intel", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--linked-servers", "--traverse-linked-servers",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Linked servers: 2" in res.output and "Reachable: 2" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["linked_servers"]["traversed"] is True
