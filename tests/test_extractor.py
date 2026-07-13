"""Extractor metadata parsing, with pyodbc mocked out (no live SQL Server)."""
from sqldoc import extractor
from sqldoc.extractor import build_connection_string
from conftest import FakeConnection


def test_build_connection_string_has_driver_and_parts():
    cs = build_connection_string("host", "DB", "user", "pw")
    assert "ODBC Driver 18 for SQL Server" in cs
    assert "SERVER=host" in cs and "DATABASE=DB" in cs
    assert "UID=user" in cs and "PWD={pw}" in cs  # password is brace-quoted
    assert "TrustServerCertificate=yes" in cs


def test_extract_metadata_parses_columns(monkeypatch, fake_table_rows):
    monkeypatch.setattr(extractor, "get_connection", lambda cs: FakeConnection(fake_table_rows))
    tables = extractor.extract_metadata("ignored-conn-str")

    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("Sales", "Orders", 1596)
    assert [c.name for c in t.columns] == ["Id", "CustomerID", "LineTotal", "Status"]

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


def test_extract_metadata_parses_default_and_fk_actions(monkeypatch, fake_table_rows):
    monkeypatch.setattr(extractor, "get_connection", lambda cs: FakeConnection(fake_table_rows))
    t = extractor.extract_metadata("cs")[0]
    by_name = {c.name: c for c in t.columns}
    assert by_name["Status"].default_definition == "((0))"
    assert by_name["CustomerID"].fk_on_delete == "CASCADE"
    assert by_name["CustomerID"].fk_on_update == "NO_ACTION"
    assert by_name["Id"].default_definition is None


def test_extract_metadata_parses_check_and_unique(monkeypatch, fake_table_rows):
    monkeypatch.setattr(extractor, "get_connection", lambda cs: FakeConnection(fake_table_rows))
    t = extractor.extract_metadata("cs")[0]
    assert len(t.check_constraints) == 1
    chk = t.check_constraints[0]
    assert chk.name == "CK_Orders_Status" and chk.column == "Status"
    assert "Status" in chk.definition
    assert len(t.unique_constraints) == 1
    assert t.unique_constraints[0].columns == ["CustomerID"]
