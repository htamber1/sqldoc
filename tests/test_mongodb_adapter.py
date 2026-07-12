"""MongoDB adapter: detection, schema inference from sampling, stats, indexes."""
import builtins
import pytest

from sqldoc.adapters import detect_dialect, get_adapter, DIALECTS
from sqldoc.adapters.mongodb import MongoAdapter, infer_schema, _bson_type


# --- fakes mimicking pymongo ------------------------------------------------

class _FakeColl:
    def __init__(self, docs, indexes):
        self._docs, self._indexes = docs, indexes

    def aggregate(self, pipeline):
        return list(self._docs)

    def index_information(self):
        return self._indexes


class _FakeDB:
    def __init__(self, name, coll_infos, colls, stats):
        self.name = name
        self._infos, self._colls, self._stats = coll_infos, colls, stats

    def list_collections(self):
        return self._infos

    def __getitem__(self, name):
        return self._colls[name]

    def command(self, cmd, name):
        return self._stats.get(name, {})


class _FakeClient:
    def __init__(self, db):
        self._db = db

    def __getitem__(self, name):
        return self._db

    def get_default_database(self):
        return self._db

    def close(self):
        pass


@pytest.fixture
def mongo_client():
    orders = _FakeColl(
        docs=[
            {"_id": "ObjId1", "customer": "Acme", "total": 42.5, "tags": ["a", "b"]},
            {"_id": "ObjId2", "customer": "Beta", "total": 10, "shipped": True},  # total is int here, +shipped
        ],
        indexes={"_id_": {"key": [("_id", 1)]},
                 "customer_1": {"key": [("customer", 1)], "unique": True}})
    db = _FakeDB(
        "shop",
        coll_infos=[{"name": "orders", "type": "collection"},
                    {"name": "active_orders", "type": "view",
                     "options": {"viewOn": "orders", "pipeline": [{"$match": {"shipped": True}}]}}],
        colls={"orders": orders},
        stats={"orders": {"count": 2, "avgObjSize": 128, "storageSize": 20480, "nindexes": 2}})
    return _FakeClient(db)


def _adapter(client):
    return MongoAdapter("mongodb://u:p@host:27017/shop", connect=lambda cs: client)


def test_detection_and_registration():
    assert detect_dialect("mongodb://u:p@host:27017/db") == "mongodb"
    assert detect_dialect("mongodb+srv://u:p@cluster.mongodb.net/db") == "mongodb"
    assert DIALECTS["mongodb"] is MongoAdapter
    a = get_adapter("mongodb://h/db", "mongodb")
    assert a.dialect == "mongodb"


def test_bson_type_and_infer_schema():
    assert _bson_type("x") == "string" and _bson_type(5) == "int"
    assert _bson_type(1.5) == "double" and _bson_type([1]) == "array" and _bson_type(None) == "null"
    cols = infer_schema([{"_id": 1, "a": "x"}, {"_id": 2, "a": 3, "b": True}])
    by = {c.name: c for c in cols}
    assert by["_id"].is_primary_key
    assert by["a"].data_type == "int | string"       # mixed types across docs
    assert by["b"].is_nullable                        # missing in the first doc


def test_extract_metadata_collection_as_table(mongo_client):
    tables = _adapter(mongo_client).extract_metadata()
    assert len(tables) == 1                            # the view is excluded
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("shop", "orders", 2)
    assert "[MongoDB: 2 docs, avg doc 128B, storage 0.0 MB, 2 indexes]" in t.description
    by = {c.name: c for c in t.columns}
    assert by["total"].data_type == "double | int"    # inferred mixed
    assert by["shipped"].is_nullable                   # only in one doc
    # indexes
    names = {i.name: i for i in t.indexes}
    assert names["customer_1"].is_unique and names["_id_"].is_primary_key


def test_extract_views_and_no_procedures(mongo_client):
    a = _adapter(mongo_client)
    views = a.extract_views()
    assert len(views) == 1 and views[0].name == "active_orders"
    assert "View on orders" in views[0].definition
    assert a.extract_procedures() == []


def test_missing_driver_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "pymongo":
            raise ImportError("no pymongo")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError) as ei:
        MongoAdapter("mongodb://h/db").connect()
    assert "sqldoc[mongodb]" in str(ei.value)
