"""Regression tests for the CMS estate fixes (sqldoc v3.1.0 field validation).

Covers four defects found running `cms discover` + `health --cms` / `secure --cms`
against a live mixed-version CMS estate:

1. `cms discover` saved the inventory to the *unresolved* `--config` path, so a
   config living in a parent directory was shadowed by a new CWD-local file that
   lost `driver:`/`windows_auth:` -- breaking the very next `--cms` command.
2. The `--cms` bulk path printed a bare ODBC `IM002` per server and never showed
   the `driver:` hint the single-server path has had since v3.0.
3. A `--cms` run without `--database` silently connects to `master`, so every
   DB_ID()-scoped detector describes `master` rather than the real databases.
4. `_w_health` never passed `tables`, so the two metadata-only detectors were
   skipped yet still reported as `0` -- indistinguishable from a genuine zero.

Run with:  pytest test_cms_estate_fixes.py
"""
import os

import pytest
from click.testing import CliRunner

from sqldoc import cli as cli_mod
from sqldoc import cms as cms_mod
from sqldoc import cms_bulk


# --- helpers ---------------------------------------------------------------

def _inventory():
    inv = cms_mod.CmsInventory(cms_server="cms-host")
    inv.groups = [cms_mod.CmsGroup(id=1, name="GroupA", parent_id=None, path="GroupA")]
    inv.servers = [
        cms_mod.CmsServer(name="srv-a", server_name="srv-a.example.test",
                          group_id=1, group_path="GroupA"),
        cms_mod.CmsServer(name="srv-b", server_name="srv-b.example.test",
                          group_id=1, group_path="GroupA"),
    ]
    return inv


def _cfg_with_inventory():
    return {"cms_servers": cms_mod.to_config(_inventory())}


# --- 1. discover writes back to the config it actually read ----------------

def test_discover_saves_to_resolved_parent_config(tmp_path, monkeypatch):
    """The inventory must land in the parent .sqldoc.yml that supplied `driver:`,
    not in a brand-new file in the CWD that then shadows it."""
    import yaml

    parent = tmp_path / "proj"
    child = parent / "sub"
    child.mkdir(parents=True)
    parent_cfg = parent / ".sqldoc.yml"
    parent_cfg.write_text("driver: ODBC Driver 17 for SQL Server\nwindows_auth: true\n",
                          encoding="utf-8")

    monkeypatch.setattr(cms_mod, "discover_live", lambda *a, **k: _inventory())
    monkeypatch.chdir(child)

    result = CliRunner().invoke(
        cli_mod.cli, ["cms", "discover", "--server", "cms-host", "--windows-auth",
                      "--output", str(child / "tree.html")])
    assert result.exit_code == 0, result.output

    # The parent config was updated in place...
    data = yaml.safe_load(parent_cfg.read_text(encoding="utf-8"))
    assert "cms_servers" in data
    assert len(data["cms_servers"]["servers"]) == 2
    # ...and its existing settings survived the merge.
    assert data["driver"] == "ODBC Driver 17 for SQL Server"
    assert data["windows_auth"] is True
    # ...and no shadowing config was created in the CWD.
    assert not (child / ".sqldoc.yml").exists()


def test_discover_honours_explicit_config_path(tmp_path, monkeypatch):
    """An explicit --config is written as given (never parent-resolved).

    NOTE: deliberately explicit rather than relying on the bare default. The
    default `.sqldoc.yml` is resolved by walking parents all the way to the
    filesystem root, so a test that ran `discover` with the default in a temp
    directory would find -- and rewrite -- whatever real config happens to live
    in an ancestor of the temp dir (e.g. the developer's home directory).
    """
    import yaml

    work = tmp_path / "fresh"
    work.mkdir()
    target = work / "custom.yml"
    target.write_text("driver: ODBC Driver 17 for SQL Server\n", encoding="utf-8")
    monkeypatch.setattr(cms_mod, "discover_live", lambda *a, **k: _inventory())
    monkeypatch.chdir(work)

    result = CliRunner().invoke(
        cli_mod.cli, ["cms", "discover", "--server", "cms-host", "--windows-auth",
                      "--config", str(target), "--output", str(work / "tree.html")])
    assert result.exit_code == 0, result.output
    assert target.exists()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert len(data["cms_servers"]["servers"]) == 2
    assert data["driver"] == "ODBC Driver 17 for SQL Server"   # merge preserved it
    assert not (work / ".sqldoc.yml").exists()


# --- 2. the bulk path explains IM002 --------------------------------------

_IM002 = ("InterfaceError: ('IM002', '[IM002] [Microsoft][ODBC Driver Manager] "
          "Data source name not found and no default driver specified (0) (SQLDriverConnect)')")


def test_bulk_run_shows_driver_hint_on_im002(tmp_path, monkeypatch, capsys):
    """A driver mismatch fails every server; the hint must appear (once)."""
    def fake_run_bulk(inv, command, opts, group=None, max_workers=8):
        return [cms_bulk.ServerResult(server=s.name, host=s.server_name,
                                      group=s.group_path, ok=False, error=_IM002)
                for s in inv.servers]

    monkeypatch.setattr(cms_bulk, "run_bulk", fake_run_bulk)
    monkeypatch.chdir(tmp_path)

    cli_mod.run_cms_bulk("health", _cfg_with_inventory(), True, None, None, None,
                         None, None, 8)
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "ODBC driver in the connection string is not installed" in combined
    # Exactly one hint, not one per failed server.
    assert combined.count("`sqldoc doctor` lists the installed ones") == 1


def test_bulk_run_no_hint_for_unrelated_errors(tmp_path, monkeypatch, capsys):
    def fake_run_bulk(inv, command, opts, group=None, max_workers=8):
        return [cms_bulk.ServerResult(server=s.name, host=s.server_name,
                                      group=s.group_path, ok=False,
                                      error="OperationalError: login timeout expired")
                for s in inv.servers]

    monkeypatch.setattr(cms_bulk, "run_bulk", fake_run_bulk)
    monkeypatch.chdir(tmp_path)

    cli_mod.run_cms_bulk("health", _cfg_with_inventory(), True, None, None, None,
                         None, None, 8)
    combined = "".join(capsys.readouterr())
    assert "ODBC driver in the connection string is not installed" not in combined


# --- 3. the `master` fallback is announced --------------------------------

def _bulk_output(command, database, tmp_path, monkeypatch, capsys):
    def fake_run_bulk(inv, cmd, opts, group=None, max_workers=8):
        return [cms_bulk.ServerResult(server=s.name, host=s.server_name,
                                      group=s.group_path, ok=True, summary={"issues": 0})
                for s in inv.servers]

    monkeypatch.setattr(cms_bulk, "run_bulk", fake_run_bulk)
    monkeypatch.chdir(tmp_path)
    cli_mod.run_cms_bulk(command, _cfg_with_inventory(), True, None, database, None,
                         None, None, 8)
    return "".join(capsys.readouterr())


@pytest.mark.parametrize("command", ["health", "quality", "doc", "scan", "intel", "comply"])
def test_db_scoped_bulk_warns_when_database_omitted(command, tmp_path, monkeypatch, capsys):
    assert "connecting to 'master'" in _bulk_output(command, None, tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize("command", ["secure", "server", "backup"])
def test_instance_scoped_bulk_does_not_warn(command, tmp_path, monkeypatch, capsys):
    """`secure`/`server`/`backup` are instance-wide; `master` is the right target."""
    assert "connecting to 'master'" not in _bulk_output(command, None, tmp_path, monkeypatch, capsys)


def test_no_warning_when_database_supplied(tmp_path, monkeypatch, capsys):
    out = _bulk_output("health", "AppDb", tmp_path, monkeypatch, capsys)
    assert "connecting to 'master'" not in out


# --- 4. health's metadata detectors actually run in bulk mode -------------

def test_w_health_passes_tables_and_reports_database(monkeypatch):
    """`duplicate_tables`/`redundant_indexes` are only computed when `tables` is
    passed; reporting them as 0 without running them is a false negative."""
    seen = {}

    class _FakeAdapter:
        dialect = "sqlserver"

    def fake_adapter_for(server, opts, database=None):
        return _FakeAdapter()

    def fake_tables(adapter, opts):
        return ["t1", "t2"]

    def fake_collect_health(adapter, *args, **kwargs):
        seen["tables"] = kwargs.get("tables")
        return object()

    def fake_summarize(report):
        return {"issues": 0, "duplicate_tables": 0, "redundant_indexes": 0}

    monkeypatch.setattr(cms_bulk, "_adapter_for", fake_adapter_for)
    monkeypatch.setattr(cms_bulk, "_tables", fake_tables)
    monkeypatch.setitem(__import__("sys").modules, "sqldoc.health",
                        type("m", (), {"collect_health": staticmethod(fake_collect_health),
                                       "summarize": staticmethod(fake_summarize)}))

    out = cms_bulk._w_health(_inventory().servers[0], {"database": "AppDb"})

    assert seen["tables"] == ["t1", "t2"], "collect_health must receive the extracted schema"
    assert out["database"] == "AppDb", "the profiled database must be reported"


def test_w_health_defaults_database_label_to_master(monkeypatch):
    monkeypatch.setattr(cms_bulk, "_adapter_for", lambda s, o, database=None: object())
    monkeypatch.setattr(cms_bulk, "_tables", lambda a, o: [])
    monkeypatch.setitem(__import__("sys").modules, "sqldoc.health",
                        type("m", (), {"collect_health": staticmethod(lambda *a, **k: object()),
                                       "summarize": staticmethod(lambda r: {"issues": 0})}))
    out = cms_bulk._w_health(_inventory().servers[0], {})
    assert out["database"] == "master"
