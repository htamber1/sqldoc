"""SnowflakeAdapter with a token-routed fake cursor (mock-only — no live account).

Snowflake exposes tables/columns/views/procs via INFORMATION_SCHEMA and keys via
SHOW PRIMARY/IMPORTED KEYS; the fake routes each by a distinctive SQL token."""
import builtins
import pytest

from sqldoc.adapters.snowflake import SnowflakeAdapter, _parse_arguments
from conftest import FakeRow


class _SfCursor:
    def __init__(self, data):
        self._data = data
        self._key = None

    def execute(self, sql, params=None):
        s = sql.upper()
        if "SHOW PRIMARY KEYS" in s:
            self._key = "pk"
        elif "SHOW IMPORTED KEYS" in s:
            self._key = "fk"
        elif "INFORMATION_SCHEMA.TABLES" in s:
            self._key = "tables"
        elif "INFORMATION_SCHEMA.VIEWS" in s:
            self._key = "views"
        elif "INFORMATION_SCHEMA.PROCEDURES" in s:
            self._key = "procs"
        elif "INFORMATION_SCHEMA.COLUMNS" in s:
            self._key = "columns"
        else:
            self._key = "unknown"
        return self

    def fetchall(self):
        return self._data.get(self._key, [])


class _SfConn:
    def __init__(self, data):
        self._data = data

    def cursor(self, *a, **k):
        return _SfCursor(self._data)

    def close(self):
        pass


@pytest.fixture
def sf_rows():
    return {
        "tables": [FakeRow(schema_name="PUBLIC", table_name="ORDERS", row_count=1500)],
        "pk": [FakeRow(schema_name="PUBLIC", table_name="ORDERS", column_name="ORDER_ID")],
        "fk": [FakeRow(fk_schema_name="PUBLIC", fk_table_name="ORDERS",
                       fk_column_name="CUSTOMER_ID", pk_table_name="CUSTOMERS",
                       pk_column_name="CUSTOMER_ID", delete_rule="NO ACTION",
                       update_rule="NO ACTION")],
        "columns": [
            FakeRow(column_name="ORDER_ID", data_type="NUMBER", max_length=None,
                    is_nullable="NO", column_default=None, description="PK"),
            FakeRow(column_name="CUSTOMER_ID", data_type="NUMBER", max_length=None,
                    is_nullable="YES", column_default=None, description=None),
            FakeRow(column_name="NOTE", data_type="TEXT", max_length=16777216,
                    is_nullable="YES", column_default="''", description="free text"),
        ],
        "views": [FakeRow(schema_name="PUBLIC", view_name="ACTIVE_ORDERS",
                          definition="SELECT * FROM ORDERS WHERE STATUS = 1")],
        "procs": [FakeRow(schema_name="PUBLIC", proc_name="GET_ORDER",
                          argument_signature="(P_ID NUMBER, P_NAME VARCHAR)",
                          description="Fetch an order")],
    }


def _adapter(sf_rows):
    return SnowflakeAdapter("snowflake://u:p@acct/DB/PUBLIC",
                            connect=lambda cs: _SfConn(sf_rows))


def test_parse_arguments():
    ps = _parse_arguments("(P_ID NUMBER, P_NAME VARCHAR)")
    assert [(p.name, p.data_type) for p in ps] == [("P_ID", "NUMBER"), ("P_NAME", "VARCHAR")]
    assert _parse_arguments("()") == []
    assert _parse_arguments("") == []


def test_extract_metadata(sf_rows):
    tables = _adapter(sf_rows).extract_metadata()
    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("PUBLIC", "ORDERS", 1500)
    by = {c.name: c for c in t.columns}
    assert by["ORDER_ID"].is_primary_key is True
    assert by["ORDER_ID"].description == "PK"
    assert by["CUSTOMER_ID"].is_foreign_key is True
    assert by["CUSTOMER_ID"].references_table == "CUSTOMERS"
    assert by["CUSTOMER_ID"].references_column == "CUSTOMER_ID"
    assert by["NOTE"].default_definition == "''"
    # Snowflake has no indexes or triggers
    assert t.indexes == [] and t.triggers == []


def test_extract_views(sf_rows):
    views = _adapter(sf_rows).extract_views()
    assert len(views) == 1
    assert views[0].name == "ACTIVE_ORDERS"
    assert [c.name for c in views[0].columns] == ["ORDER_ID", "CUSTOMER_ID", "NOTE"]
    assert views[0].definition.startswith("SELECT")


def test_extract_procedures(sf_rows):
    procs = _adapter(sf_rows).extract_procedures()
    assert len(procs) == 1
    p = procs[0]
    assert (p.schema, p.name) == ("PUBLIC", "GET_ORDER")
    assert [pm.name for pm in p.parameters] == ["P_ID", "P_NAME"]


def test_build_connection_string():
    cs = SnowflakeAdapter.build_connection_string("myacct", "MYDB", "u", "pw")
    assert cs == "snowflake://u:pw@myacct/MYDB"


def test_missing_driver_gives_actionable_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("snowflake"):
            raise ImportError("No module named 'snowflake'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = SnowflakeAdapter("snowflake://u:p@acct/DB/PUBLIC")
    with pytest.raises(ImportError) as ei:
        adapter.connect()
    assert "sqldoc[snowflake]" in str(ei.value)
    assert "snowflake-connector-python" in str(ei.value)
