"""The adapter layer: dialect auto-detection, the registry, capabilities, and
that the SqlServerAdapter drives extraction through an injectable connector
(the seam the extractor shim relies on for tests)."""
import pytest

from sqldoc.adapters import (
    get_adapter, detect_dialect, UnsupportedDialectError,
    SqlServerAdapter, PostgresAdapter, MySQLAdapter, SqliteAdapter, SnowflakeAdapter,
    DatabaseAdapter, Capabilities,
    DIALECTS, SUPPORTED_DIALECTS, PLANNED_DIALECTS, DIALECT_CHOICES,
)
from conftest import FakeConnection


# --- auto-detection --------------------------------------------------------

@pytest.mark.parametrize("cs, expected", [
    ("DRIVER={ODBC Driver 18 for SQL Server};SERVER=x;DATABASE=d;UID=u;PWD=p", "sqlserver"),
    ("DRIVER={ODBC Driver 18 for SQL Server};SERVER=foo.database.windows.net;DATABASE=d", "azuresql"),
    ("postgresql://user:pw@host:5432/db", "postgres"),
    ("postgres://user:pw@host/db", "postgres"),
    ("mysql://user:pw@host/db", "mysql"),
    ("", "sqlserver"),
    (None, "sqlserver"),
])
def test_detect_dialect(cs, expected):
    assert detect_dialect(cs) == expected


def test_azuresql_host_beats_sqlserver_driver():
    # An Azure host with the SQL Server ODBC driver should read as azuresql.
    cs = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=myapp.database.windows.net;DATABASE=d"
    assert detect_dialect(cs) == "azuresql"


# --- registry --------------------------------------------------------------

def test_all_dialects_supported():
    assert set(SUPPORTED_DIALECTS) == {
        "sqlserver", "azuresql", "postgres", "mysql", "sqlite", "snowflake"}
    assert PLANNED_DIALECTS == []
    assert set(DIALECT_CHOICES) == set(DIALECTS)


def test_registry_maps_to_expected_adapters():
    # Azure SQL speaks the same T-SQL, so it maps to the SQL Server adapter
    # (a regression guard: existing Azure-via-connection-string users keep working).
    assert DIALECTS["azuresql"] is SqlServerAdapter
    assert DIALECTS["postgres"] is PostgresAdapter
    assert DIALECTS["mysql"] is MySQLAdapter
    assert DIALECTS["sqlite"] is SqliteAdapter
    assert DIALECTS["snowflake"] is SnowflakeAdapter


# --- get_adapter -----------------------------------------------------------

def test_get_adapter_auto_detects_sqlserver():
    a = get_adapter("DRIVER={ODBC Driver 18 for SQL Server};SERVER=x;DATABASE=d")
    assert isinstance(a, SqlServerAdapter)
    assert a.dialect == "sqlserver"


def test_get_adapter_explicit_dialect_overrides_detection():
    # A postgres-looking URL forced to sqlserver still yields the SQL Server adapter.
    a = get_adapter("postgresql://u:p@h/db", dialect="sqlserver")
    assert isinstance(a, SqlServerAdapter)


def test_get_adapter_azuresql():
    a = get_adapter("SERVER=x.database.windows.net;DATABASE=d")
    assert isinstance(a, SqlServerAdapter)


def test_get_adapter_postgres():
    a = get_adapter("postgresql://u:p@h/db")
    assert isinstance(a, PostgresAdapter)
    assert a.dialect == "postgres"


def test_get_adapter_mysql():
    a = get_adapter("mysql://u:p@h/db")
    assert isinstance(a, MySQLAdapter)
    assert a.dialect == "mysql"


def test_get_adapter_unknown_dialect_raises():
    with pytest.raises(UnsupportedDialectError) as ei:
        get_adapter("whatever", dialect="oracle")
    assert "Unknown dialect" in str(ei.value)


# --- capabilities ----------------------------------------------------------

def test_sqlserver_capabilities_full():
    caps = SqlServerAdapter.capabilities
    assert isinstance(caps, Capabilities)
    # SQL Server is the reference impl: everything the commands need is supported.
    for flag in ("documentation", "quality", "health", "access_audit",
                 "data_lineage", "pii_scan", "intel", "insights"):
        assert getattr(caps, flag) is True


def test_capabilities_defaults_conservative():
    # A bare Capabilities() only turns on the dialect-neutral pieces; the
    # dialect-specific features default off so new adapters opt in deliberately.
    caps = Capabilities()
    assert caps.documentation is True
    assert caps.health is False
    assert caps.quality is False
    assert caps.access_audit is False


# --- extraction through an injected connector ------------------------------

def test_sqlserver_adapter_uses_injected_connector(fake_table_rows):
    """extract_metadata should read through the injected connect() — the seam
    the extractor shim uses so tests never touch a real driver."""
    adapter = SqlServerAdapter("cs", connect=lambda cs: FakeConnection(fake_table_rows))
    tables = adapter.extract_metadata()
    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("Sales", "Orders", 1596)
    assert [c.name for c in t.columns] == ["Id", "CustomerID", "LineTotal", "Status"]


def test_build_connection_string_is_sqlserver_odbc():
    cs = SqlServerAdapter.build_connection_string("host", "DB", "user", "pw")
    assert "ODBC Driver 18 for SQL Server" in cs
    assert "SERVER=host" in cs and "DATABASE=DB" in cs
    assert "UID=user" in cs and "PWD=pw" in cs


def test_adapter_abc_cannot_instantiate():
    with pytest.raises(TypeError):
        DatabaseAdapter("cs")


# --- shim still delegates to the adapter -----------------------------------

def test_extractor_shim_delegates(monkeypatch, fake_table_rows):
    from sqldoc import extractor
    monkeypatch.setattr(extractor, "get_connection", lambda cs: FakeConnection(fake_table_rows))
    # The shim resolves get_connection from its namespace at call time, so the
    # monkeypatch reaches the adapter's connect().
    tables = extractor.extract_metadata("ignored")
    assert tables[0].name == "Orders"
