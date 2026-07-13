"""Phase/feature — live CMS bulk + group + executive pipeline against the Docker
SQL Server. Rather than mutate msdb's CMS tables, we build an in-memory inventory
whose registered servers all point at the live instance (SQL auth), then run the
real bulk workers, group filtering, and estate rollup against it. Skip-gated."""
import re

import pytest

from _live import MSSQL_CS, requires_mssql

pytestmark = [requires_mssql, pytest.mark.integration]


def _sa():
    m = re.search(r'UID=([^;]+);PWD=([^;]+)', MSSQL_CS)
    return m.group(1), m.group(2)


def _inventory():
    """Two 'registered servers' in two groups, both pointing at the live host."""
    from sqldoc.cms import CmsInventory, CmsGroup, CmsServer
    groups = [
        CmsGroup(id=1, name="DatabaseEngineServerGroup", parent_id=None, is_system=True, path=""),
        CmsGroup(id=2, name="Production", parent_id=1, path="Production"),
        CmsGroup(id=3, name="West", parent_id=2, path="Production/West"),
        CmsGroup(id=4, name="Development", parent_id=1, path="Development"),
    ]
    servers = [
        CmsServer(name="prod-a", server_name="localhost", group_id=2, group_path="Production"),
        CmsServer(name="prod-west", server_name="localhost", group_id=3, group_path="Production/West"),
        CmsServer(name="dev-a", server_name="localhost", group_id=4, group_path="Development"),
    ]
    return CmsInventory(cms_server="localhost", groups=groups, servers=servers)


def _opts(database="AdventureWorks2022"):
    u, p = _sa()
    return {"windows_auth": False, "username": u, "password": p, "database": database}


# --- bulk against the live estate ------------------------------------------

def test_bulk_health_across_estate():
    from sqldoc.cms_bulk import run_bulk
    results = run_bulk(_inventory(), "health", _opts(), max_workers=3)
    assert len(results) == 3 and all(r.ok for r in results), \
        [r.error for r in results if not r.ok]
    # real health metrics came back
    assert all("issues" in r.summary for r in results)


def test_bulk_scan_finds_real_pii():
    from sqldoc.cms_bulk import run_bulk
    results = run_bulk(_inventory(), "scan", _opts(), max_workers=3)
    assert all(r.ok for r in results)
    assert all(r.summary["total"] >= 0 for r in results)
    assert any(r.summary["total"] > 0 for r in results)     # AdventureWorks has PII


# --- nested group filtering (live) -----------------------------------------

def test_group_filter_nested():
    from sqldoc.cms_bulk import run_bulk
    # "Production" includes its nested "West" subgroup -> prod-a + prod-west
    prod = run_bulk(_inventory(), "health", _opts(), group="Production", max_workers=2)
    assert {r.server for r in prod} == {"prod-a", "prod-west"}
    # a leaf group -> just that server
    west = run_bulk(_inventory(), "health", _opts(), group="Production/West")
    assert {r.server for r in west} == {"prod-west"}
    dev = run_bulk(_inventory(), "health", _opts(), group="Development")
    assert {r.server for r in dev} == {"dev-a"}


# --- estate executive (live) -----------------------------------------------

def test_estate_executive():
    from sqldoc.cms_executive import collect_estate, build_estate_json
    est = collect_estate(_inventory(), _opts(), max_workers=3)
    assert est.server_count == 3
    assert est.database_count >= 3          # >= 1 user db per server (AdventureWorks)
    j = build_estate_json(est)
    assert j["report_type"] == "cms-executive" and len(j["servers"]) == 3


# --- failure isolation (live + bad server) ---------------------------------

def test_inventory_report_live():
    from sqldoc.cms_report import collect_report, build_report_json
    results = collect_report(_inventory(), _opts(database="master"), max_workers=3)
    assert len(results) == 3 and all(r.ok for r in results), \
        [r.error for r in results if not r.ok]
    # real SQL Server metadata came back
    r0 = results[0].summary
    assert r0["version"] and "SQL" in r0["edition"] or r0["edition"]
    assert r0["db_count"] >= 1 and r0["uptime_hours"] >= 0
    j = build_report_json(_inventory(), results)
    assert j["reachable"] == 3


def test_failure_isolated_with_one_bad_server():
    from sqldoc.cms import CmsInventory, CmsServer
    from sqldoc.cms_bulk import run_bulk
    inv = CmsInventory(cms_server="localhost", groups=[], servers=[
        CmsServer(name="good", server_name="localhost", group_path=""),
        CmsServer(name="bad", server_name="no-such-host-xyz", group_path=""),
    ])
    results = run_bulk(inv, "secure", _opts(database="master"), max_workers=2)
    by = {r.server: r for r in results}
    assert by["good"].ok and not by["bad"].ok      # one fails, the other still runs
    assert by["bad"].error
