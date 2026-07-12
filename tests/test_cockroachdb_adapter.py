"""CockroachDB adapter: detection, PG inheritance, zone configs + localities."""
from sqldoc.adapters import detect_dialect, get_adapter, DIALECTS
from sqldoc.adapters.cockroachdb import CockroachDBAdapter
from sqldoc.adapters.postgres import PostgresAdapter
from conftest import FakeConnection, FakeRow


def _adapter(rows):
    return CockroachDBAdapter("postgresql://u:p@cluster.abc.cockroachlabs.cloud:26257/db",
                              connect=lambda cs: FakeConnection(rows))


def test_detection_and_registration():
    # a cockroachlabs.cloud host wins even with a postgresql:// scheme
    assert detect_dialect("postgresql://u:p@x.cockroachlabs.cloud:26257/db") == "cockroachdb"
    assert detect_dialect("cockroachdb://u:p@host/db") == "cockroachdb"
    # a plain postgres URL is still postgres
    assert detect_dialect("postgresql://u:p@host/db") == "postgres"
    assert DIALECTS["cockroachdb"] is CockroachDBAdapter
    assert issubclass(CockroachDBAdapter, PostgresAdapter)
    a = get_adapter("cockroachdb://h/db", "cockroachdb")
    assert a.dialect == "cockroachdb" and not a.capabilities.health


def test_zone_configs():
    rows = {"crdb_zones": [
        FakeRow(target="DATABASE db", raw_config_sql="ALTER DATABASE db CONFIGURE ZONE USING num_replicas = 5"),
        FakeRow(target="TABLE db.public.orders", raw_config_sql="ALTER TABLE ... num_replicas = 3"),
    ]}
    zc = _adapter(rows).crdb_zone_configs()
    assert zc[0]["target"] == "DATABASE db" and "num_replicas = 5" in zc[0]["config"]
    assert len(zc) == 2


def test_localities():
    rows = {"crdb_nodes": [
        FakeRow(node_id=1, locality="region=us-east1,zone=us-east1-b"),
        FakeRow(node_id=2, locality="region=us-west1,zone=us-west1-a"),
    ]}
    locs = _adapter(rows).crdb_localities()
    assert locs[0]["node_id"] == 1 and "us-east1" in locs[0]["locality"]
    assert len(locs) == 2
