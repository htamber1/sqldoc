"""Extractor metadata parsing, with pyodbc mocked out (no live SQL Server)."""
from sqldoc import extractor
from sqldoc.extractor import build_connection_string
from conftest import FakeConnection


def test_build_connection_string_has_driver_and_parts():
    cs = build_connection_string("host", "DB", "user", "pw")
    assert "ODBC Driver 18 for SQL Server" in cs
    assert "SERVER=host" in cs and "DATABASE=DB" in cs
    assert "UID=user" in cs and "PWD=pw" in cs
    assert "TrustServerCertificate=yes" in cs


def test_extract_metadata_parses_columns(monkeypatch, fake_table_rows):
    monkeypatch.setattr(extractor, "get_connection", lambda cs: FakeConnection(fake_table_rows))
    tables = extractor.extract_metadata("ignored-conn-str")

    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("Sales", "Orders", 1596)
    assert [c.name for c in t.columns] == ["Id", "CustomerID", "LineTotal"]

    by_name = {c.name: c for c in t.columns}
    assert by_name["Id"].is_primary_key is True
    assert by_name["Id"].description == "Order id"
    assert by_name["CustomerID"].is_foreign_key is True
    assert by_name["CustomerID"].references_table == "Customer"
    assert by_name["CustomerID"].references_column == "Id"


def test_extract_metadata_parses_computed_column(monkeypatch, fake_table_rows):
    monkeypatch.setattr(extractor, "get_connection", lambda cs: FakeConnection(fake_table_rows))
    t = extractor.extract_metadata("cs")[0]
    lt = next(c for c in t.columns if c.name == "LineTotal")
    assert lt.is_computed is True
    assert lt.computed_definition == "([Qty]*[Price])"


def test_extract_metadata_groups_indexes(monkeypatch, fake_table_rows):
    monkeypatch.setattr(extractor, "get_connection", lambda cs: FakeConnection(fake_table_rows))
    t = extractor.extract_metadata("cs")[0]
    idx = {i.name: i for i in t.indexes}
    assert set(idx) == {"PK_Orders", "IX_Orders_Customer"}
    assert idx["PK_Orders"].is_primary_key is True
    # key vs included columns are separated
    assert idx["IX_Orders_Customer"].key_columns == ["CustomerID"]
    assert idx["IX_Orders_Customer"].included_columns == ["LineTotal"]


def test_extract_metadata_parses_triggers(monkeypatch, fake_table_rows):
    monkeypatch.setattr(extractor, "get_connection", lambda cs: FakeConnection(fake_table_rows))
    t = extractor.extract_metadata("cs")[0]
    assert len(t.triggers) == 1
    tr = t.triggers[0]
    assert tr.name == "trOrders"
    assert tr.is_instead_of is False
    assert tr.events == ["INSERT", "UPDATE"]
    assert tr.definition.startswith("CREATE TRIGGER")
