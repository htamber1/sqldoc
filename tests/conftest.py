"""Shared fixtures + a tiny fake-pyodbc layer so tests never touch a real DB."""
import pytest

from sqldoc.extractor import (
    Table, Column, Index, Trigger, View, Parameter, StoredProcedure,
    CheckConstraint, UniqueConstraint,
)


# --- In-memory schema fixtures (no database required) ----------------------

def build_tables():
    """A fresh two-table schema exercising PK/FK/computed columns, an index,
    and a trigger. Returned fresh each call so tests can mutate freely."""
    orders = Table(
        schema="Sales",
        name="Orders",
        row_count=1596,
        columns=[
            Column("Id", "int", 4, False, True, False, None, None, description="Order id"),
            Column("CustomerID", "int", 4, True, False, True, "Customer", "Id",
                   fk_on_delete="CASCADE", fk_on_update="NO_ACTION"),
            Column("LineTotal", "money", 8, True, False, False, None, None,
                   is_computed=True, computed_definition="([Qty]*[Price])"),
            Column("Status", "int", 4, False, False, False, None, None,
                   default_definition="((0))"),
        ],
        indexes=[Index("PK_Orders", "CLUSTERED", True, True, ["Id"], [])],
        triggers=[Trigger("trOrders", False, False, ["INSERT", "UPDATE"],
                          "CREATE TRIGGER [Sales].[trOrders] ON [Sales].[Orders] AFTER INSERT AS BEGIN SET NOCOUNT ON; END;")],
        check_constraints=[CheckConstraint("CK_Orders_Status", "([Status]>=(0))", "Status")],
        unique_constraints=[UniqueConstraint("UQ_Orders_Customer", ["CustomerID"])],
    )
    archive = Table(
        schema="Sales",
        name="Archive",
        row_count=0,
        columns=[Column("Id", "int", 4, False, True, False, None, None)],
    )
    return [orders, archive]


def build_views():
    return [View(
        schema="Sales",
        name="vActiveOrders",
        columns=[Column("Id", "int", 4, False, False, False, None, None),
                 Column("CustomerID", "int", 4, True, False, False, None, None)],
        definition="CREATE VIEW [Sales].[vActiveOrders] AS SELECT Id, CustomerID FROM Sales.Orders WHERE Total > 0;",
    )]


def build_procs():
    return [StoredProcedure(
        schema="Sales",
        name="uspGetOrder",
        parameters=[Parameter("@OrderId", "int", 4, False),
                    Parameter("@Total", "money", 8, True)],
        definition="CREATE PROCEDURE [Sales].[uspGetOrder] @OrderId int, @Total money OUTPUT AS BEGIN SELECT 1; END;",
    )]


@pytest.fixture
def sample_tables():
    return build_tables()


@pytest.fixture
def sample_views():
    return build_views()


@pytest.fixture
def sample_procs():
    return build_procs()


# --- Fake pyodbc for extractor tests ---------------------------------------

class FakeRow:
    """Supports both attribute access (row.column_name) and tuple unpacking,
    like a pyodbc.Row."""
    def __init__(self, **kw):
        object.__setattr__(self, "_d", kw)

    def __getattr__(self, k):
        return self._d[k]

    def __iter__(self):
        return iter(self._d.values())

    def __getitem__(self, i):
        return list(self._d.values())[i]


class FakeCursor:
    def __init__(self, data):
        self._data = data
        self._last = None

    def execute(self, sql, *params):
        # Health DMV queries first — the dead-tables query aliases `p.rows AS
        # row_count`, which would otherwise misroute to the extractor branch.
        if "dm_exec_query_stats" in sql:
            self._last = "slow"
        elif "dm_db_index_usage_stats" in sql:
            self._last = "dead"
        elif "dm_db_missing_index_details" in sql:
            self._last = "missing"
        elif "dm_db_index_physical_stats" in sql:
            self._last = "frag"
        elif "row_count" in sql:
            self._last = "tables"
        elif "trigger_name" in sql:
            self._last = "triggers"
        elif "is_computed" in sql:
            self._last = "columns"
        elif "index_name" in sql:
            self._last = "indexes"
        elif "check_definition" in sql:
            self._last = "checks"
        elif "uq_name" in sql:
            self._last = "uniques"
        elif "sys.views v" in sql and "view_name" in sql:
            self._last = "views"
        elif "proc_name" in sql:
            self._last = "procs"
        elif "sys.parameters" in sql:
            self._last = "params"
        else:
            self._last = "unknown"
        return self

    def fetchall(self):
        return self._data.get(self._last, [])


class FakeConnection:
    def __init__(self, data):
        self._data = data

    def cursor(self):
        return FakeCursor(self._data)

    def close(self):
        pass


@pytest.fixture
def fake_health_rows():
    """Rows the health DMV queries would see."""
    return {
        "slow": [FakeRow(query_text="SELECT * FROM Sales.Orders WHERE Total > 0",
                         execution_count=1200, total_elapsed_ms=90000.0,
                         avg_elapsed_ms=75.0, avg_logical_reads=4200,
                         last_execution_time="2026-07-10 09:00:00")],
        "dead": [
            FakeRow(schema_name="Sales", table_name="Archive", row_count=50000,
                    user_seeks=0, user_scans=0, user_lookups=0, user_updates=1200,
                    last_user_scan=None),                     # dead: writes, no reads
            FakeRow(schema_name="Sales", table_name="Orders", row_count=1596,
                    user_seeks=900, user_scans=3, user_lookups=1, user_updates=40,
                    last_user_scan="2026-07-10 08:00:00"),    # active: filtered out
            FakeRow(schema_name="Sales", table_name="Empty", row_count=0,
                    user_seeks=0, user_scans=0, user_lookups=0, user_updates=0,
                    last_user_scan=None),                     # empty: filtered out
        ],
        "missing": [FakeRow(schema_name="Sales", table_name="Orders",
                            equality_columns="[CustomerID]", inequality_columns="[OrderDate]",
                            included_columns="[Total]", user_seeks=800,
                            avg_user_impact=92.5, improvement_measure=14200.7)],
        "frag": [
            FakeRow(schema_name="Sales", table_name="Orders", index_name="IX_Orders_Customer",
                    avg_fragmentation_in_percent=64.2, page_count=5000),   # REBUILD
            FakeRow(schema_name="Sales", table_name="Orders", index_name="IX_Orders_Date",
                    avg_fragmentation_in_percent=18.0, page_count=800),    # REORGANIZE
        ],
    }


@pytest.fixture
def fake_table_rows():
    """Rows a single-table extract would see from the catalog views."""
    return {
        "tables": [FakeRow(schema="Sales", table="Orders", rows=1596)],
        "triggers": [FakeRow(
            schema_name="Sales", table_name="Orders", trigger_name="trOrders",
            is_instead_of_trigger=0, is_disabled=0,
            definition="CREATE TRIGGER trOrders ...", events="INSERT,UPDATE",
        )],
        "columns": [
            FakeRow(column_name="Id", data_type="int", max_length=4, is_nullable=0,
                    is_primary_key=1, is_foreign_key=0, references_table=None,
                    references_column=None, description="Order id",
                    is_computed=0, computed_definition=None, default_definition=None,
                    fk_on_delete=None, fk_on_update=None),
            FakeRow(column_name="CustomerID", data_type="int", max_length=4, is_nullable=1,
                    is_primary_key=0, is_foreign_key=1, references_table="Customer",
                    references_column="Id", description=None,
                    is_computed=0, computed_definition=None, default_definition=None,
                    fk_on_delete="CASCADE", fk_on_update="NO_ACTION"),
            FakeRow(column_name="LineTotal", data_type="money", max_length=8, is_nullable=1,
                    is_primary_key=0, is_foreign_key=0, references_table=None,
                    references_column=None, description=None,
                    is_computed=1, computed_definition="([Qty]*[Price])", default_definition=None,
                    fk_on_delete=None, fk_on_update=None),
            FakeRow(column_name="Status", data_type="int", max_length=4, is_nullable=0,
                    is_primary_key=0, is_foreign_key=0, references_table=None,
                    references_column=None, description=None,
                    is_computed=0, computed_definition=None, default_definition="((0))",
                    fk_on_delete=None, fk_on_update=None),
        ],
        "checks": [
            FakeRow(check_name="CK_Orders_Status", check_definition="([Status]>=(0))",
                    column_name="Status"),
        ],
        "uniques": [
            FakeRow(uq_name="UQ_Orders_Customer", column_name="CustomerID"),
        ],
        "indexes": [
            FakeRow(index_name="PK_Orders", type_desc="CLUSTERED", is_unique=1,
                    is_primary_key=1, column_name="Id", is_included_column=0, key_ordinal=1),
            FakeRow(index_name="IX_Orders_Customer", type_desc="NONCLUSTERED", is_unique=0,
                    is_primary_key=0, column_name="CustomerID", is_included_column=0, key_ordinal=1),
            FakeRow(index_name="IX_Orders_Customer", type_desc="NONCLUSTERED", is_unique=0,
                    is_primary_key=0, column_name="LineTotal", is_included_column=1, key_ordinal=0),
        ],
    }
