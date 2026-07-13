"""CMS discovery, group traversal, config round-trip, and rendering.
Fake cursor — no live SQL Server."""
import json

import pytest

from sqldoc import cms
from sqldoc.cms import (discover_inventory, connection_string_for, servers_in_group,
                        select_servers, to_config, from_config, save_cms_servers, has_inventory)
from sqldoc.cms_renderer import render_tree_text, render_tree_html, build_inventory_json


class FakeCursor:
    def __init__(self, data):
        self.data, self._rows = data, []

    def execute(self, sql, *a):
        self._rows = []
        for token, rows in self.data.items():
            if token in sql:
                self._rows = rows
                return

    def fetchall(self):
        return self._rows


DATA = {
    "CMS_GROUPS": [
        {"server_group_id": 1, "name": "DatabaseEngineServerGroup", "parent_id": None,
         "description": "", "is_system_object": 1},
        {"server_group_id": 2, "name": "Production", "parent_id": 1,
         "description": "prod estate", "is_system_object": 0},
        {"server_group_id": 3, "name": "West", "parent_id": 2, "description": "",
         "is_system_object": 0},
        {"server_group_id": 4, "name": "Development", "parent_id": 1, "description": "",
         "is_system_object": 0},
    ],
    "CMS_SERVERS": [
        {"server_id": 10, "server_group_id": 2, "name": "prod-sql1",
         "server_name": "prod-sql1.corp", "description": "OLTP"},
        {"server_id": 11, "server_group_id": 3, "name": "prod-west1",
         "server_name": "prod-west1.corp", "description": ""},
        {"server_id": 12, "server_group_id": 4, "name": "dev-sql1",
         "server_name": "dev-sql1.corp", "description": ""},
    ],
}


@pytest.fixture
def inv():
    return discover_inventory(FakeCursor(DATA), "CMS01")


# --- discovery + paths -----------------------------------------------------

def test_discover_counts(inv):
    assert inv.cms_server == "CMS01"
    assert len(inv.servers) == 3
    assert len([g for g in inv.groups if not g.is_system]) == 3


def test_group_paths(inv):
    by_name = {g.name: g for g in inv.groups}
    assert by_name["Production"].path == "Production"        # system root omitted
    assert by_name["West"].path == "Production/West"         # nested
    assert by_name["Development"].path == "Development"


def test_server_group_paths(inv):
    by_name = {s.name: s for s in inv.servers}
    assert by_name["prod-west1"].group_path == "Production/West"
    assert by_name["prod-sql1"].server_name == "prod-sql1.corp"


# --- connection building ---------------------------------------------------

def test_connection_string_windows():
    cs = connection_string_for("host1", database="master", windows_auth=True)
    assert "Trusted_Connection=yes" in cs and "UID=" not in cs and "SERVER=host1" in cs


def test_connection_string_sql():
    cs = connection_string_for("host1", windows_auth=False, username="u", password="p")
    assert "UID=u" in cs and "PWD=p" in cs and "Trusted_Connection" not in cs


def test_connection_string_falls_back_to_windows_without_user():
    cs = connection_string_for("host1", windows_auth=False, username=None)
    assert "Trusted_Connection=yes" in cs


# --- group traversal -------------------------------------------------------

def test_servers_in_group_nested(inv):
    prod = {s.name for s in servers_in_group(inv, "Production", recursive=True)}
    assert prod == {"prod-sql1", "prod-west1"}          # includes nested West


def test_servers_in_group_non_recursive(inv):
    prod = {s.name for s in servers_in_group(inv, "Production", recursive=False)}
    assert prod == {"prod-sql1"}


def test_servers_in_group_by_path(inv):
    west = {s.name for s in servers_in_group(inv, "Production/West")}
    assert west == {"prod-west1"}


def test_servers_in_group_unknown(inv):
    assert servers_in_group(inv, "Nope") == []


def test_select_servers_all_vs_group(inv):
    assert len(select_servers(inv)) == 3
    assert len(select_servers(inv, "Development")) == 1


# --- config round-trip -----------------------------------------------------

def test_config_round_trip(inv):
    cfg = {"cms_servers": to_config(inv)}
    assert has_inventory(cfg)
    inv2 = from_config(cfg)
    assert len(inv2.servers) == 3
    assert {s.name for s in inv2.servers} == {"prod-sql1", "prod-west1", "dev-sql1"}
    assert servers_in_group(inv2, "Production", recursive=True)     # traversal survives round-trip


def test_save_cms_servers_merges(tmp_path, inv):
    import yaml
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"server": "keepme", "cms": {"server": "CMS01"}}), encoding="utf-8")
    save_cms_servers(str(p), inv)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["server"] == "keepme"          # existing keys preserved
    assert data["cms"]["server"] == "CMS01"
    assert len(data["cms_servers"]["servers"]) == 3


# --- rendering -------------------------------------------------------------

def test_render_tree_text(inv):
    text = render_tree_text(inv)
    assert "[Production]" in text and "[West]" in text and "prod-sql1" in text


def test_render_tree_html_offline(tmp_path, inv):
    from sqldoc.offline import verify_file
    out = tmp_path / "cms.html"
    render_tree_html(inv, str(out))
    html = out.read_text(encoding="utf-8")
    assert "Production" in html and "prod-sql1" in html
    assert verify_file(str(out)) == []


def test_build_inventory_json(inv):
    j = build_inventory_json(inv)
    assert j["report_type"] == "cms-inventory"
    assert j["server_count"] == 3 and j["group_count"] == 3


# --- CLI -------------------------------------------------------------------

def test_cli_cms_discover(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    monkeypatch.setattr(cms, "discover_live",
                        lambda server, **k: discover_inventory(FakeCursor(DATA), server))
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"cms": {"server": "CMS01", "windows_auth": True}}), encoding="utf-8")
    out = tmp_path / "inv.html"
    js = tmp_path / "inv.json"
    res = CliRunner().invoke(cli.cli, ["cms", "discover", "--config", str(p),
                                       "--output", str(out), "--json", str(js)])
    assert res.exit_code == 0, res.output
    assert "prod-sql1" in res.output and "Production" in res.output
    # inventory saved back to the config
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert len(data["cms_servers"]["servers"]) == 3
    assert json.loads(js.read_text())["server_count"] == 3
