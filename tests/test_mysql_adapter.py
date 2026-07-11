"""MySQLAdapter extraction, with a token-routed fake cursor (no live DB)."""
import builtins
import pytest

from sqldoc.adapters.mysql import MySQLAdapter
from conftest import FakeRow


class _MyCursor:
    def __init__(self, data):
        self._data = data
        self._key = None

    def execute(self, sql, params=None):
        s = sql
        if "table_rows" in s:
            self._key = "tables"
        elif "event_manipulation" in s:
            self._key = "triggers"
        elif "column_key" in s:
            self._key = "tcolumns"
        elif "referential_constraints" in s:
            self._key = "fk"
        elif "seq_in_index" in s:
            self._key = "indexes"
        elif "check_clause" in s:
            self._key = "checks"
        elif "'UNIQUE'" in s:
            self._key = "uniques"
        elif "view_definition" in s:
            self._key = "views"
        elif "information_schema.routines" in s:
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


class _MyConn:
    def __init__(self, data):
        self._data = data

    def cursor(self, *a, **k):     # accepts named_tuple=True
        return _MyCursor(self._data)

    def close(self):
        pass


@pytest.fixture
def my_rows():
    return {
        "tables": [FakeRow(schema_name="shop", table_name="orders", row_count=1500)],
        "triggers": [FakeRow(schema_name="shop", table_name="orders",
                             trigger_name="trg_orders", action_timing="AFTER",
                             event_manipulation="INSERT", action_statement="BEGIN END")],
        "tcolumns": [
            FakeRow(column_name="id", data_type="int", max_length=None, is_nullable="NO",
                    column_default=None, column_key="PRI", generation_expression="",
                    description="Order id"),
            FakeRow(column_name="customer_id", data_type="int", max_length=None, is_nullable="YES",
                    column_default=None, column_key="MUL", generation_expression="", description=None),
            FakeRow(column_name="line_total", data_type="decimal", max_length=None, is_nullable="YES",
                    column_default=None, column_key="", generation_expression="(`qty` * `price`)",
                    description=None),
            FakeRow(column_name="status", data_type="int", max_length=None, is_nullable="NO",
                    column_default="0", column_key="", generation_expression="", description=None),
        ],
        "fk": [FakeRow(column_name="customer_id", referenced_table_name="customers",
                       referenced_column_name="id", delete_rule="CASCADE", update_rule="NO ACTION")],
        "indexes": [
            FakeRow(index_name="PRIMARY", non_unique=0, seq_in_index=1,
                    column_name="id", index_type="BTREE"),
            FakeRow(index_name="ix_orders_customer", non_unique=1, seq_in_index=1,
                    column_name="customer_id", index_type="BTREE"),
        ],
        "checks": [FakeRow(constraint_name="orders_chk_1", check_clause="(`status` >= 0)")],
        "uniques": [FakeRow(uq_name="uq_customer", column_name="customer_id")],
        "views": [FakeRow(schema_name="shop", view_name="active_orders",
                          definition="select `id` from `orders`")],
        "vcolumns": [
            FakeRow(column_name="id", data_type="int", max_length=None, is_nullable="NO"),
            FakeRow(column_name="customer_id", data_type="int", max_length=None, is_nullable="YES"),
        ],
        "procs": [FakeRow(schema_name="shop", proc_name="get_order", specific_name="get_order",
                          definition="BEGIN END", description="Fetch an order")],
        "params": [
            FakeRow(parameter_name="p_id", data_type="int", max_length=None, parameter_mode="IN"),
            FakeRow(parameter_name="p_total", data_type="decimal", max_length=None, parameter_mode="OUT"),
        ],
    }


def _adapter(my_rows):
    return MySQLAdapter("mysql://u:p@h/shop", connect=lambda cs: _MyConn(my_rows))


def test_extract_metadata_table(my_rows):
    tables = _adapter(my_rows).extract_metadata()
    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("shop", "orders", 1500)
    assert [c.name for c in t.columns] == ["id", "customer_id", "line_total", "status"]


def test_extract_metadata_keys_computed_default(my_rows):
    t = _adapter(my_rows).extract_metadata()[0]
    by = {c.name: c for c in t.columns}
    assert by["id"].is_primary_key is True
    assert by["id"].description == "Order id"
    assert by["customer_id"].is_foreign_key is True
    assert by["customer_id"].references_table == "customers"
    assert by["customer_id"].fk_on_delete == "CASCADE"
    assert by["line_total"].is_computed is True
    assert by["line_total"].computed_definition == "(`qty` * `price`)"
    assert by["status"].default_definition == "0"


def test_extract_metadata_indexes(my_rows):
    t = _adapter(my_rows).extract_metadata()[0]
    idx = {i.name: i for i in t.indexes}
    assert set(idx) == {"PRIMARY", "ix_orders_customer"}
    assert idx["PRIMARY"].is_primary_key is True
    assert idx["PRIMARY"].is_unique is True
    assert idx["ix_orders_customer"].is_unique is False
    assert idx["ix_orders_customer"].key_columns == ["customer_id"]
    assert idx["ix_orders_customer"].included_columns == []   # MySQL has no INCLUDE


def test_extract_metadata_checks_uniques_triggers(my_rows):
    t = _adapter(my_rows).extract_metadata()[0]
    assert [c.name for c in t.check_constraints] == ["orders_chk_1"]
    assert t.unique_constraints[0].columns == ["customer_id"]
    assert len(t.triggers) == 1
    tr = t.triggers[0]
    assert tr.events == ["INSERT"]
    assert tr.is_instead_of is False and tr.is_disabled is False


def test_extract_views(my_rows):
    views = _adapter(my_rows).extract_views()
    assert len(views) == 1
    v = views[0]
    assert (v.schema, v.name) == ("shop", "active_orders")
    assert [c.name for c in v.columns] == ["id", "customer_id"]


def test_extract_procedures(my_rows):
    procs = _adapter(my_rows).extract_procedures()
    assert len(procs) == 1
    p = procs[0]
    assert (p.schema, p.name) == ("shop", "get_order")
    assert [pm.name for pm in p.parameters] == ["p_id", "p_total"]
    assert p.parameters[1].is_output is True


def test_build_connection_string():
    assert MySQLAdapter.build_connection_string("h", "db", "u", "pw") == "mysql://u:pw@h/db"


def test_missing_driver_gives_actionable_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("mysql"):
            raise ImportError("No module named 'mysql'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = MySQLAdapter("mysql://u:p@h/db")  # no injected connector
    with pytest.raises(ImportError) as ei:
        adapter.connect()
    assert "sqldoc[mysql]" in str(ei.value)
    assert "mysql-connector-python" in str(ei.value)
