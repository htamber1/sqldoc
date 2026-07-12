"""Azure Synapse adapter: detection, distribution enrichment, workload groups."""
from sqldoc.adapters import detect_dialect, get_adapter, DIALECTS
from sqldoc.adapters.synapse import SynapseAdapter
from conftest import FakeConnection, FakeRow


def _adapter(rows):
    return SynapseAdapter("Server=ws.sql.azuresynapse.net;Database=pool;",
                          connect=lambda cs: FakeConnection(rows))


def test_detection_and_registration():
    assert detect_dialect("Server=ws.sql.azuresynapse.net;Database=pool") == "synapse"
    assert DIALECTS["synapse"] is SynapseAdapter
    a = get_adapter("Server=ws.sql.azuresynapse.net;", "synapse")
    assert a.dialect == "synapse" and not a.capabilities.health


def test_distribution_enriches_table_descriptions(fake_table_rows):
    rows = {**fake_table_rows, "synapse_dist": [
        FakeRow(schema_name="Sales", table_name="Orders", distribution="HASH",
                dist_column="CustomerID", skew_pct=42.5),
    ]}
    tables = _adapter(rows).extract_metadata()
    orders = next(t for t in tables if t.name == "Orders")
    assert "[Distribution: HASH on CustomerID, skew 42.5%]" in orders.description


def test_distribution_round_robin_no_column(fake_table_rows):
    rows = {**fake_table_rows, "synapse_dist": [
        FakeRow(schema_name="Sales", table_name="Orders", distribution="ROUND_ROBIN",
                dist_column=None, skew_pct=0.3),
    ]}
    tables = _adapter(rows).extract_metadata()
    orders = next(t for t in tables if t.name == "Orders")
    assert "[Distribution: ROUND_ROBIN]" in orders.description   # no column, skew < 1 dropped


def test_synapse_distribution_map(fake_table_rows):
    rows = {**fake_table_rows, "synapse_dist": [
        FakeRow(schema_name="Sales", table_name="Orders", distribution="REPLICATE",
                dist_column=None, skew_pct=None)]}
    dist = _adapter(rows).synapse_distribution()
    assert dist[("Sales", "Orders")][0] == "REPLICATE"


def test_workload_groups():
    rows = {"synapse_workload": [
        FakeRow(group_name="largerc", importance="high", min_percentage_resource=25,
                cap_percentage_resource=100, request_min_resource_grant_percent=25,
                query_execution_timeout_sec=0),
        FakeRow(group_name="smallrc", importance="normal", min_percentage_resource=0,
                cap_percentage_resource=100, request_min_resource_grant_percent=3,
                query_execution_timeout_sec=0),
    ]}
    wg = _adapter(rows).synapse_workload_groups()
    assert wg[0]["group"] == "largerc" and wg[0]["concurrency_slots"] == 4   # 100/25
    assert wg[1]["concurrency_slots"] == 33                                  # 100/3
