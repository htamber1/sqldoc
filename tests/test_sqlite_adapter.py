"""SqliteAdapter against a real temp-file SQLite database (stdlib sqlite3, so no
mocking and no external dependency)."""
import sqlite3

import pytest

from sqldoc.adapters.sqlite import SqliteAdapter


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "shop.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE customer (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            status INTEGER DEFAULT 0
        );
        CREATE TABLE "order" (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
            total NUMERIC
        );
        CREATE INDEX idx_order_customer ON "order"(customer_id);
        CREATE VIEW active_customers AS SELECT id, name FROM customer WHERE status = 1;
        CREATE TRIGGER trg_order_ai AFTER INSERT ON "order"
        BEGIN UPDATE customer SET status = 1 WHERE id = NEW.customer_id; END;
        INSERT INTO customer (name, email, status) VALUES ('Ann', 'a@x.com', 1), ('Bob', 'b@x.com', 0);
        INSERT INTO "order" (customer_id, total) VALUES (1, 9.5);
    """)
    conn.commit()
    conn.close()
    return str(p)


def test_extract_metadata_tables(db_path):
    tables = SqliteAdapter(db_path).extract_metadata()
    assert sorted(t.name for t in tables) == ["customer", "order"]
    assert all(t.schema == "main" for t in tables)
    cust = next(t for t in tables if t.name == "customer")
    assert cust.row_count == 2


def test_pk_fk_default(db_path):
    tables = SqliteAdapter(db_path).extract_metadata()
    order = next(t for t in tables if t.name == "order")
    by = {c.name: c for c in order.columns}
    assert by["id"].is_primary_key is True
    assert by["customer_id"].is_foreign_key is True
    assert by["customer_id"].references_table == "customer"
    assert by["customer_id"].references_column == "id"
    assert by["customer_id"].fk_on_delete == "CASCADE"
    cust = next(t for t in tables if t.name == "customer")
    status = next(c for c in cust.columns if c.name == "status")
    assert status.default_definition == "0"
    assert next(c for c in cust.columns if c.name == "name").is_nullable is False


def test_indexes_and_uniques(db_path):
    tables = SqliteAdapter(db_path).extract_metadata()
    order = next(t for t in tables if t.name == "order")
    assert any(i.name == "idx_order_customer" and i.key_columns == ["customer_id"]
               for i in order.indexes)
    cust = next(t for t in tables if t.name == "customer")
    # the UNIQUE(email) constraint surfaces as a unique constraint
    assert any(uq.columns == ["email"] for uq in cust.unique_constraints)


def test_triggers(db_path):
    tables = SqliteAdapter(db_path).extract_metadata()
    order = next(t for t in tables if t.name == "order")
    assert len(order.triggers) == 1
    tr = order.triggers[0]
    assert tr.name == "trg_order_ai"
    assert tr.events == ["INSERT", "UPDATE"]   # UPDATE appears in the trigger body


def test_views_and_no_procedures(db_path):
    a = SqliteAdapter(db_path)
    views = a.extract_views()
    assert len(views) == 1
    assert views[0].name == "active_customers"
    assert [c.name for c in views[0].columns] == ["id", "name"]
    assert a.extract_procedures() == []


def test_build_connection_string_is_path():
    assert SqliteAdapter.build_connection_string("srv", "/data/app.db", "u", "p") == "/data/app.db"


@pytest.mark.parametrize("cs, expect_suffix", [
    ("/data/app.db", "app.db"),
    ("sqlite:///data/app.db", "app.db"),
    ("sqlite://rel.db", "rel.db"),
    ("file:x.db", "x.db"),
])
def test_db_path_parsing(cs, expect_suffix):
    from sqldoc.adapters.sqlite import _db_path
    assert _db_path(cs).endswith(expect_suffix)
