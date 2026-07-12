"""Amazon Redshift adapter: detection, table-info enrichment, WLM, recs."""
from sqldoc.adapters import detect_dialect, get_adapter, DIALECTS
from sqldoc.adapters.redshift import RedshiftAdapter
from sqldoc.adapters.postgres import PostgresAdapter
from sqldoc.extractor import Table, Column
from conftest import FakeConnection, FakeRow


def _adapter(rows):
    return RedshiftAdapter("redshift://u:p@cl.abc.us-east-1.redshift.amazonaws.com:5439/db",
                           connect=lambda cs: FakeConnection(rows))


def test_detection_and_registration():
    assert detect_dialect("host=cl.abc.us-east-1.redshift.amazonaws.com") == "redshift"
    assert detect_dialect("redshift://u:p@host:5439/db") == "redshift"
    assert DIALECTS["redshift"] is RedshiftAdapter
    assert issubclass(RedshiftAdapter, PostgresAdapter)
    a = get_adapter("redshift://h/db", "redshift")
    assert a.dialect == "redshift" and not a.capabilities.health


def test_table_info():
    rows = {"rs_tableinfo": [
        FakeRow(schema_name="public", table_name="orders", diststyle="KEY(customer_id)",
                sortkey1="order_date", skew_rows=3.2, unsorted=12.5, tbl_rows=1000000),
    ]}
    info = _adapter(rows).redshift_table_info()
    d = info[("public", "orders")]
    assert d["diststyle"] == "KEY(customer_id)" and d["sortkey"] == "order_date"
    assert d["skew"] == 3.2 and d["unsorted"] == 12.5


def test_enrich_adds_distribution_tag():
    tables = [Table("public", "orders", 100, columns=[Column("id", "int", 4, True, False, False, None, None)])]
    info = {("public", "orders"): {"diststyle": "EVEN", "sortkey": "id",
                                   "skew": 4.0, "unsorted": 20.0}}
    RedshiftAdapter._enrich(tables, info)
    assert "[Redshift: DISTSTYLE EVEN, SORTKEY id, skew 4.0, unsorted 20.0%]" in tables[0].description


def test_wlm_queues():
    rows = {"rs_wlm": [
        FakeRow(service_class=6, slots=5, query_working_mem=1024),
        FakeRow(service_class=7, slots=15, query_working_mem=512),
    ]}
    wlm = _adapter(rows).redshift_wlm_queues()
    assert wlm[0]["service_class"] == 6 and wlm[0]["concurrency_slots"] == 5


def test_recommendations():
    rows = {"rs_alerts": [
        FakeRow(event="Missing query planner statistics", solution="Run the ANALYZE command", occurrences=42),
        FakeRow(event="Scanned a large number of deleted rows", solution="Run the VACUUM command", occurrences=8),
    ]}
    recs = _adapter(rows).redshift_recommendations()
    assert recs[0]["occurrences"] == 42 and "ANALYZE" in recs[0]["solution"]
    assert any("VACUUM" in r["solution"] for r in recs)
