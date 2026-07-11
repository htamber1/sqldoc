"""Database adapters — one metadata contract, many dialects.

`get_adapter(connection_string, dialect=None)` is the single entry point: it
picks a concrete `DatabaseAdapter` by explicit dialect or by auto-detecting from
the connection string, and raises `UnsupportedDialectError` for a dialect that
is recognized but not yet implemented.

Today SQL Server (and Azure SQL, which speaks the same T-SQL) are implemented;
`postgres` and `mysql` are recognized for auto-detection and flagged as planned
(v1.5.0) so a mistyped/early connection string fails loud rather than silently
running T-SQL against the wrong engine.
"""
from sqldoc.adapters.base import DatabaseAdapter, Capabilities
from sqldoc.adapters.sqlserver import SqlServerAdapter
from sqldoc.adapters.postgres import PostgresAdapter
from sqldoc.adapters.mysql import MySQLAdapter


class UnsupportedDialectError(Exception):
    """Raised for a dialect that is recognized but has no adapter yet."""


# Registry: dialect name -> adapter class (None == recognized but not built).
# Azure SQL reuses the SQL Server adapter (identical T-SQL); a dedicated adapter
# with graceful DMV degradation for Azure SQL Database can follow. The postgres
# and mysql drivers are optional dependencies imported lazily by their adapters,
# so importing this registry never requires them to be installed.
DIALECTS: dict = {
    "sqlserver": SqlServerAdapter,
    "azuresql": SqlServerAdapter,
    "postgres": PostgresAdapter,
    "mysql": MySQLAdapter,
}

# What --dialect accepts, ordered supported-first for help text / error messages.
SUPPORTED_DIALECTS = [name for name, cls in DIALECTS.items() if cls is not None]
PLANNED_DIALECTS = [name for name, cls in DIALECTS.items() if cls is None]
DIALECT_CHOICES = list(DIALECTS.keys())


def detect_dialect(connection_string: str) -> str:
    """Best-effort dialect guess from a connection string.

    Detects by URL scheme (`postgresql://`, `mysql://`), by driver name, and by
    the Azure SQL host suffix. Falls back to `sqlserver` (the historical
    default), so an ordinary ODBC connection string keeps its behavior.
    """
    cs = (connection_string or "").lower()
    if "database.windows.net" in cs:
        return "azuresql"
    if cs.startswith(("postgresql://", "postgres://")) or "psql odbc" in cs or "postgresql" in cs:
        return "postgres"
    if cs.startswith("mysql://") or "mysql" in cs:
        return "mysql"
    return "sqlserver"


def get_adapter(connection_string: str, dialect: str = None,
                connect=None) -> DatabaseAdapter:
    """Resolve a `DatabaseAdapter` for the connection.

    `dialect` (from --dialect / config) overrides auto-detection. Raises
    `UnsupportedDialectError` for an unknown or not-yet-implemented dialect.
    `connect` injects a connector (used by the extractor shim for tests).
    """
    name = (dialect or detect_dialect(connection_string) or "sqlserver").lower()
    if name not in DIALECTS:
        raise UnsupportedDialectError(
            f"Unknown dialect '{name}'. Choose one of: {', '.join(DIALECT_CHOICES)}."
        )
    cls = DIALECTS[name]
    if cls is None:
        raise UnsupportedDialectError(
            f"Dialect '{name}' is recognized but not supported yet "
            f"(planned for v1.5.0). Currently supported: {', '.join(SUPPORTED_DIALECTS)}."
        )
    return cls(connection_string, connect=connect)


__all__ = [
    "DatabaseAdapter", "Capabilities", "SqlServerAdapter",
    "PostgresAdapter", "MySQLAdapter",
    "UnsupportedDialectError", "DIALECTS", "SUPPORTED_DIALECTS",
    "PLANNED_DIALECTS", "DIALECT_CHOICES", "detect_dialect", "get_adapter",
]
