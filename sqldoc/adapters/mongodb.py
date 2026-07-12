"""MongoDB adapter.

MongoDB is schemaless, so this adapter treats each **collection as a pseudo-table**
and **infers a schema by sampling documents** (which fields appear, their BSON
types, and whether they are always present). It also surfaces:

* **Index configuration** (``collection.index_information()``).
* **Collection stats** — document count, average document size, storage size,
  and index count (``collStats``) — folded into the collection description.

Views (``viewOn`` collections) become views. There are no stored procedures.
Uses the ``pymongo`` driver (optional). Detected from a ``mongodb://`` or
``mongodb+srv://`` scheme.

NOTE: mock-tested only — not run against a live MongoDB.
"""
from urllib.parse import urlparse

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities, Table, Column, Index, View, StoredProcedure,
)

_TYPE_NAMES = {
    "str": "string", "int": "int", "float": "double", "bool": "bool",
    "dict": "object", "list": "array", "bytes": "binData", "NoneType": "null",
    "ObjectId": "objectId", "datetime": "date", "date": "date",
    "Decimal128": "decimal", "Int64": "long",
}


def _bson_type(v) -> str:
    if v is None:
        return "null"
    return _TYPE_NAMES.get(type(v).__name__, type(v).__name__)


def infer_schema(docs) -> list:
    """Infer columns from sampled documents: field name, seen BSON type(s),
    nullability (missing in some docs or explicitly null)."""
    n = len(docs) or 1
    fields = {}   # name -> {"types": set, "present": int}
    order = []
    for d in docs:
        for k, v in d.items():
            if k not in fields:
                fields[k] = {"types": set(), "present": 0}
                order.append(k)
            fields[k]["types"].add(_bson_type(v))
            fields[k]["present"] += 1
    columns = []
    for name in order:
        info = fields[name]
        types = sorted(t for t in info["types"] if t != "null")
        dtype = " | ".join(types) or "null"
        nullable = info["present"] < n or "null" in info["types"]
        columns.append(Column(
            name=name, data_type=dtype, max_length=None, is_nullable=nullable,
            is_primary_key=(name == "_id"), is_foreign_key=False,
            references_table=None, references_column=None))
    return columns


class MongoAdapter(DatabaseAdapter):
    dialect = "mongodb"
    display_name = "MongoDB"
    capabilities = Capabilities(quality=False, health=False, access_audit=False,
                                triggers=False, computed_columns=False, check_constraints=False)
    sample_size = 100

    @staticmethod
    def _default_connect(connection_string: str):
        try:
            import pymongo
        except ImportError as e:
            raise ImportError(
                "MongoDB support requires the 'pymongo' driver, which is not installed. "
                "Install it with:  pip install sqldoc[mongodb]  (or:  pip install pymongo)."
            ) from e
        return pymongo.MongoClient(connection_string)

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        auth = f"{username}:{password}@" if username else ""
        return f"mongodb://{auth}{server}/{database}"

    def _db_name(self):
        u = urlparse(self.connection_string)
        return (u.path or "").lstrip("/").split("?")[0] or None

    def _db(self, client):
        name = self._db_name()
        if name:
            return client[name]
        try:
            return client.get_default_database()
        except Exception:
            return client["test"]

    def _sample(self, coll):
        try:
            docs = list(coll.aggregate([{"$sample": {"size": self.sample_size}}]))
            if docs:
                return docs
        except Exception:
            pass
        try:
            return list(coll.find(limit=self.sample_size))
        except TypeError:
            return list(coll.find().limit(self.sample_size))

    def _collection_infos(self, db):
        try:
            return list(db.list_collections())
        except Exception:
            return [{"name": n, "type": "collection"} for n in db.list_collection_names()]

    def _indexes(self, coll) -> list:
        try:
            info = coll.index_information()
        except Exception:
            return []
        out = []
        for name, spec in info.items():
            key = spec.get("key", [])
            out.append(Index(
                name=name, type_desc="NONCLUSTERED",
                is_unique=bool(spec.get("unique")),
                is_primary_key=(name == "_id_"),
                key_columns=[f for f, _dir in key], included_columns=[]))
        return out

    def _stats_tag(self, db, name) -> tuple:
        try:
            st = db.command("collStats", name)
        except Exception:
            return 0, None
        count = int(st.get("count", 0) or 0)
        avg = st.get("avgObjSize")
        storage = st.get("storageSize")
        nindexes = st.get("nindexes")
        parts = [f"{count:,} docs"]
        if avg:
            parts.append(f"avg doc {int(avg)}B")
        if storage:
            parts.append(f"storage {round(storage / 1048576.0, 1)} MB")
        if nindexes:
            parts.append(f"{nindexes} indexes")
        return count, "[MongoDB: " + ", ".join(parts) + "]"

    # --- collections as tables ---------------------------------------------

    def extract_metadata(self) -> list[Table]:
        client = self.connect()
        db = self._db(client)
        db_name = self._db_name() or getattr(db, "name", "db")
        tables = []
        for info in self._collection_infos(db):
            if info.get("type") == "view":
                continue
            name = info["name"]
            if name.startswith("system."):
                continue
            coll = db[name]
            columns = infer_schema(self._sample(coll))
            count, tag = self._stats_tag(db, name)
            tables.append(Table(
                schema=db_name, name=name, row_count=count, columns=columns,
                indexes=self._indexes(coll), triggers=[], check_constraints=[],
                unique_constraints=[], description=tag))
        client.close() if hasattr(client, "close") else None
        return tables

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        client = self.connect()
        db = self._db(client)
        db_name = self._db_name() or getattr(db, "name", "db")
        views = []
        for info in self._collection_infos(db):
            if info.get("type") != "view":
                continue
            opts = info.get("options", {}) or {}
            definition = None
            if opts.get("viewOn"):
                definition = f"View on {opts.get('viewOn')} with pipeline {opts.get('pipeline')}"
            views.append(View(schema=db_name, name=info["name"], columns=[], definition=definition))
        client.close() if hasattr(client, "close") else None
        return views

    def extract_procedures(self) -> list[StoredProcedure]:
        return []   # MongoDB has no stored procedures
