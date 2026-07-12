"""Google BigQuery adapter: detection, client-based extraction, enrichment."""
import builtins
import pytest

from sqldoc.adapters import detect_dialect, get_adapter, DIALECTS
from sqldoc.adapters.bigquery import BigQueryAdapter


# --- fakes mimicking the google-cloud-bigquery client -----------------------

class _Field:
    def __init__(self, name, field_type, mode="NULLABLE", description=None):
        self.name, self.field_type, self.mode, self.description = name, field_type, mode, description


class _Part:
    def __init__(self, field, type_="DAY"):
        self.field, self.type_ = field, type_


class _BQTable:
    def __init__(self, table_id, schema, table_type="TABLE", num_rows=0, num_bytes=0,
                 modified=None, time_partitioning=None, clustering_fields=None,
                 view_query=None, description=None):
        self.table_id, self.schema, self.table_type = table_id, schema, table_type
        self.num_rows, self.num_bytes, self.modified = num_rows, num_bytes, modified
        self.time_partitioning = time_partitioning
        self.range_partitioning = None
        self.clustering_fields = clustering_fields
        self.view_query, self.description = view_query, description
        self.reference = self


class _Dataset:
    def __init__(self, dataset_id):
        self.dataset_id = dataset_id


class _Routine:
    def __init__(self, routine_id, body):
        self.routine_id, self.body = routine_id, body


class _FakeClient:
    def __init__(self, datasets, tables, routines=None):
        self._ds, self._tables, self._routines = datasets, tables, routines or {}

    def list_datasets(self):
        return self._ds

    def list_tables(self, dsid):
        return self._tables.get(dsid, [])

    def get_table(self, ref):
        return ref

    def list_routines(self, dsid):
        return self._routines.get(dsid, [])


@pytest.fixture
def client():
    orders = _BQTable(
        "orders",
        schema=[_Field("order_id", "INTEGER", "REQUIRED", "PK"),
                _Field("region", "STRING"),
                _Field("items", "RECORD", "REPEATED")],
        num_rows=5_000_000, num_bytes=4_509_715_660,
        modified="2026-07-10T12:00:00.000Z",
        time_partitioning=_Part("order_date", "DAY"),
        clustering_fields=["region"], description="Orders fact")
    vw = _BQTable("v_orders", schema=[_Field("order_id", "INTEGER")],
                  table_type="VIEW", view_query="SELECT * FROM sales.orders")
    return _FakeClient([_Dataset("sales")],
                       {"sales": [orders, vw]},
                       {"sales": [_Routine("fn_total", "RETURN 1")]})


def _adapter(client):
    return BigQueryAdapter("bigquery://my-project", connect=lambda cs: client)


def test_detection_and_registration():
    assert detect_dialect("bigquery://my-project") == "bigquery"
    assert DIALECTS["bigquery"] is BigQueryAdapter
    a = get_adapter("bigquery://p", "bigquery")
    assert a.dialect == "bigquery"


def test_extract_metadata(client):
    tables = _adapter(client).extract_metadata()
    assert len(tables) == 1                       # the VIEW is excluded
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("sales", "orders", 5_000_000)
    assert "partitioned by order_date (DAY)" in t.description
    assert "clustered by region" in t.description
    assert "4.2 GB" in t.description and "modified 2026-07-10" in t.description
    by = {c.name: c for c in t.columns}
    assert not by["order_id"].is_nullable          # REQUIRED
    assert by["items"].data_type == "RECORD[]"     # REPEATED


def test_extract_views(client):
    views = _adapter(client).extract_views()
    assert len(views) == 1 and views[0].name == "v_orders"
    assert views[0].definition == "SELECT * FROM sales.orders"


def test_extract_routines(client):
    procs = _adapter(client).extract_procedures()
    assert procs[0].name == "fn_total"


def test_missing_driver_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("google"):
            raise ImportError("no google")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError) as ei:
        BigQueryAdapter("bigquery://p").connect()
    assert "sqldoc[bigquery]" in str(ei.value)
