"""Databricks adapter: detection, Delta metadata, partition enrichment, details."""
import builtins
import pytest

from sqldoc.adapters import detect_dialect, get_adapter, DIALECTS
from sqldoc.adapters.databricks import DatabricksAdapter
from conftest import FakeConnection, FakeRow


def _adapter(rows):
    return DatabricksAdapter("databricks://token:xyz@adb-123.azuredatabricks.net/sql/1.0/warehouses/abc?catalog=main",
                             connect=lambda cs: FakeConnection(rows))


@pytest.fixture
def dbx_rows():
    return {
        "dbx_tables": [FakeRow(schema_name="sales", table_name="orders", comment="Order facts")],
        "dbx_pk": [FakeRow(schema_name="sales", table_name="orders", column_name="order_id")],
        "dbx_columns": [
            FakeRow(column_name="order_id", data_type="BIGINT", is_nullable="NO",
                    comment="PK", partition_index=None),
            FakeRow(column_name="order_date", data_type="DATE", is_nullable="YES",
                    comment=None, partition_index=0),        # partition column
        ],
        "dbx_views": [FakeRow(schema_name="sales", view_name="v_orders",
                              definition="SELECT * FROM sales.orders")],
        "dbx_routines": [FakeRow(schema_name="sales", proc_name="fn_total",
                                 definition="RETURN 1")],
        "dbx_history": [FakeRow(version=0), FakeRow(version=1), FakeRow(version=2)],
        "dbx_detail": [FakeRow(numFiles=5000, sizeInBytes=10737418240)],   # 10 GB, many files
    }


def test_detection_and_registration():
    assert detect_dialect("Server=adb-123.azuredatabricks.net") == "databricks"
    assert detect_dialect("databricks://token:x@h/path") == "databricks"
    assert detect_dialect("host=foo.databricks.com") == "databricks"
    assert DIALECTS["databricks"] is DatabricksAdapter
    a = get_adapter("databricks://token:x@h/p", "databricks")
    assert a.dialect == "databricks"


def test_extract_metadata_partitions_and_pk(dbx_rows):
    tables = _adapter(dbx_rows).extract_metadata()
    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name) == ("sales", "orders")
    assert "[Delta: partitioned by order_date]" in t.description and "Order facts" in t.description
    by = {c.name: c for c in t.columns}
    assert by["order_id"].is_primary_key and not by["order_date"].is_primary_key


def test_extract_views_and_procedures(dbx_rows):
    a = _adapter(dbx_rows)
    views = a.extract_views()
    assert views[0].name == "v_orders" and views[0].definition.startswith("SELECT")
    procs = a.extract_procedures()
    assert procs[0].name == "fn_total"


def test_delta_details_recommends_optimize(dbx_rows):
    d = _adapter(dbx_rows).delta_details("sales", "orders")
    assert d["version_count"] == 3 and d["num_files"] == 5000
    assert d["size_mb"] == 10240.0
    assert "OPTIMIZE" in d["recommendation"]


def test_build_connection_string():
    cs = DatabricksAdapter.build_connection_string("adb-1.azuredatabricks.net/sql/1.0/wh/x", "main", "u", "tok")
    assert cs.startswith("databricks://token:tok@adb-1.azuredatabricks.net/sql/1.0/wh/x")
    assert "catalog=main" in cs


def test_missing_driver_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "databricks" or name.startswith("databricks."):
            raise ImportError("no databricks")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError) as ei:
        DatabricksAdapter("databricks://token:x@h/p").connect()
    assert "sqldoc[databricks]" in str(ei.value)
