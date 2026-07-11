"""PostgresAdapter extraction, with a token-routed fake cursor (no live DB)."""
import builtins
import pytest

from sqldoc.adapters.postgres import PostgresAdapter, _decode_trigger_events
from conftest import FakeRow


# --- a fake psycopg2-style connection routing by SQL token -----------------

class _PgCursor:
    def __init__(self, data):
        self._data = data
        self._key = None

    def execute(self, sql, params=None):
        s = sql
        if "reltuples" in s:
            self._key = "tables"
        elif "pg_get_triggerdef" in s:
            self._key = "triggers"
        elif "generation_expression" in s:
            self._key = "tcolumns"
        elif "PRIMARY KEY" in s:
            self._key = "pk"
        elif "FOREIGN KEY" in s:
            self._key = "fk"
        elif "pg_index" in s:
            self._key = "indexes"
        elif "check_clause" in s:
            self._key = "checks"
        elif "'UNIQUE'" in s:
            self._key = "uniques"
        elif "pg_views" in s:
            self._key = "views"
        elif "pg_get_functiondef" in s:
            self._key = "procs"
        elif "information_schema.parameters" in s:
            self._key = "params"
        elif "information_schema.columns" in s:
            self._key = "vcolumns"
        else:
            self._key = "unknown"
        return self

    def fetchall(self):
        return self._data.get(self._key, [])


class _PgConn:
    def __init__(self, data):
        self._data = data

    def cursor(self, *a, **k):
        return _PgCursor(self._data)

    def close(self):
        pass


@pytest.fixture
def pg_rows():
    return {
        "tables": [FakeRow(schema_name="public", table_name="orders", row_count=1500)],
        "triggers": [FakeRow(schema_name="public", table_name="orders",
                             trigger_name="trg_orders", tgtype=5,  # ROW|INSERT
                             tgenabled="O", definition="CREATE TRIGGER trg_orders ...")],
        "tcolumns": [
            FakeRow(column_name="id", data_type="integer", max_length=None,
                    is_nullable="NO", column_default="nextval('orders_id_seq')",
                    is_generated="NEVER", generation_expression=None, description="Order id"),
            FakeRow(column_name="customer_id", data_type="integer", max_length=None,
                    is_nullable="YES", column_default=None,
                    is_generated="NEVER", generation_expression=None, description=None),
            FakeRow(column_name="line_total", data_type="numeric", max_length=None,
                    is_nullable="YES", column_default=None,
                    is_generated="ALWAYS", generation_expression="(qty * price)", description=None),
        ],
        "pk": [FakeRow(column_name="id")],
        "fk": [FakeRow(column_name="customer_id", foreign_table_name="customers",
                       foreign_column_name="id", delete_rule="CASCADE", update_rule="NO ACTION")],
        "indexes": [
            FakeRow(index_name="orders_pkey", index_type="btree", is_unique=True,
                    is_primary_key=True, column_name="id", is_included=False),
            FakeRow(index_name="ix_orders_customer", index_type="btree", is_unique=False,
                    is_primary_key=False, column_name="customer_id", is_included=False),
            FakeRow(index_name="ix_orders_customer", index_type="btree", is_unique=False,
                    is_primary_key=False, column_name="line_total", is_included=True),
        ],
        "checks": [FakeRow(constraint_name="orders_status_check", check_clause="((status >= 0))")],
        "uniques": [FakeRow(uq_name="orders_customer_key", column_name="customer_id")],
        "views": [FakeRow(schema_name="public", view_name="active_orders",
                          definition="SELECT id, customer_id FROM orders WHERE total > 0")],
        "vcolumns": [
            FakeRow(column_name="id", data_type="integer", max_length=None, is_nullable="NO"),
            FakeRow(column_name="customer_id", data_type="integer", max_length=None, is_nullable="YES"),
        ],
        "procs": [FakeRow(schema_name="public", proc_name="get_order", oid=16490,
                          definition="CREATE FUNCTION get_order ...", description="Fetch an order")],
        "params": [
            FakeRow(parameter_name="p_id", data_type="integer", max_length=None, parameter_mode="IN"),
            FakeRow(parameter_name="p_total", data_type="numeric", max_length=None, parameter_mode="OUT"),
        ],
    }


def _adapter(pg_rows):
    return PostgresAdapter("postgresql://u:p@h/db", connect=lambda cs: _PgConn(pg_rows))


# --- trigger bitmask decode ------------------------------------------------

@pytest.mark.parametrize("tgtype, expected", [
    (5, ["INSERT"]),                 # ROW|INSERT
    (1 << 4, ["UPDATE"]),
    (1 << 3, ["DELETE"]),
    ((1 << 2) | (1 << 4), ["INSERT", "UPDATE"]),
])
def test_decode_trigger_events(tgtype, expected):
    assert _decode_trigger_events(tgtype) == expected


# --- extraction ------------------------------------------------------------

def test_extract_metadata_table(pg_rows):
    tables = _adapter(pg_rows).extract_metadata()
    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("public", "orders", 1500)
    assert [c.name for c in t.columns] == ["id", "customer_id", "line_total"]


def test_extract_metadata_keys_and_computed(pg_rows):
    t = _adapter(pg_rows).extract_metadata()[0]
    by = {c.name: c for c in t.columns}
    assert by["id"].is_primary_key is True
    assert by["id"].description == "Order id"
    assert by["customer_id"].is_foreign_key is True
    assert by["customer_id"].references_table == "customers"
    assert by["customer_id"].references_column == "id"
    assert by["customer_id"].fk_on_delete == "CASCADE"
    assert by["line_total"].is_computed is True
    assert by["line_total"].computed_definition == "(qty * price)"
    assert by["id"].default_definition.startswith("nextval")


def test_extract_metadata_indexes_keys_vs_included(pg_rows):
    t = _adapter(pg_rows).extract_metadata()[0]
    idx = {i.name: i for i in t.indexes}
    assert set(idx) == {"orders_pkey", "ix_orders_customer"}
    assert idx["orders_pkey"].is_primary_key is True
    assert idx["ix_orders_customer"].key_columns == ["customer_id"]
    assert idx["ix_orders_customer"].included_columns == ["line_total"]
    assert idx["orders_pkey"].type_desc == "BTREE"


def test_extract_metadata_checks_uniques_triggers(pg_rows):
    t = _adapter(pg_rows).extract_metadata()[0]
    assert [c.name for c in t.check_constraints] == ["orders_status_check"]
    assert t.unique_constraints[0].columns == ["customer_id"]
    assert len(t.triggers) == 1
    assert t.triggers[0].events == ["INSERT"]
    assert t.triggers[0].is_instead_of is False


def test_extract_views(pg_rows):
    views = _adapter(pg_rows).extract_views()
    assert len(views) == 1
    v = views[0]
    assert (v.schema, v.name) == ("public", "active_orders")
    assert v.definition.startswith("SELECT")
    assert [c.name for c in v.columns] == ["id", "customer_id"]


def test_extract_procedures(pg_rows):
    procs = _adapter(pg_rows).extract_procedures()
    assert len(procs) == 1
    p = procs[0]
    assert (p.schema, p.name) == ("public", "get_order")
    assert [pm.name for pm in p.parameters] == ["p_id", "p_total"]
    assert p.parameters[1].is_output is True


# --- connection string + optional driver -----------------------------------

def test_build_connection_string():
    assert PostgresAdapter.build_connection_string("h", "db", "u", "pw") == \
        "postgresql://u:pw@h/db"


def test_missing_driver_gives_actionable_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("psycopg2"):
            raise ImportError("No module named 'psycopg2'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = PostgresAdapter("postgresql://u:p@h/db")  # no injected connector
    with pytest.raises(ImportError) as ei:
        adapter.connect()
    assert "sqldoc[postgres]" in str(ei.value)
    assert "psycopg2" in str(ei.value)
