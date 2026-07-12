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
from sqldoc.adapters.sqlite import SqliteAdapter
from sqldoc.adapters.snowflake import SnowflakeAdapter
from sqldoc.adapters.oracle import OracleAdapter
from sqldoc.adapters.azure_mi import AzureMiAdapter
from sqldoc.adapters.synapse import SynapseAdapter
from sqldoc.adapters.redshift import RedshiftAdapter
from sqldoc.adapters.databricks import DatabricksAdapter
from sqldoc.adapters.bigquery import BigQueryAdapter
from sqldoc.adapters.cockroachdb import CockroachDBAdapter
from sqldoc.adapters.db2 import Db2Adapter


class UnsupportedDialectError(Exception):
    """Raised for a dialect that is recognized but has no adapter yet."""


# Registry: dialect name -> adapter class (None == recognized but not built).
# Azure SQL reuses the SQL Server adapter (identical T-SQL). The postgres, mysql,
# and snowflake drivers are optional dependencies imported lazily by their
# adapters, so importing this registry never requires them to be installed
# (sqlite uses the stdlib driver, so it always works).
DIALECTS: dict = {
    "sqlserver": SqlServerAdapter,
    "azuresql": SqlServerAdapter,
    "azure_managed_instance": AzureMiAdapter,
    "synapse": SynapseAdapter,
    "redshift": RedshiftAdapter,
    "databricks": DatabricksAdapter,
    "bigquery": BigQueryAdapter,
    "cockroachdb": CockroachDBAdapter,
    "db2": Db2Adapter,
    "postgres": PostgresAdapter,
    "mysql": MySQLAdapter,
    "sqlite": SqliteAdapter,
    "snowflake": SnowflakeAdapter,
    "oracle": OracleAdapter,
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
    if "sql.azuresynapse.net" in cs or "database.windows.net/synapse" in cs:
        return "synapse"
    if "redshift.amazonaws.com" in cs or cs.startswith("redshift://"):
        return "redshift"
    if ("azuredatabricks.net" in cs or "databricks.com" in cs
            or cs.startswith("databricks://")):
        return "databricks"
    if cs.startswith("bigquery://"):
        return "bigquery"
    if cs.startswith(("db2://", "ibm-db2://")):
        return "db2"
    # CockroachDB Cloud often uses a postgresql:// scheme, so check the host
    # marker before the generic postgres branch below.
    if "cockroachlabs.cloud" in cs or cs.startswith("cockroachdb://"):
        return "cockroachdb"
    if "database.windows.net" in cs:
        # Managed Instance hosts carry an MI marker (".mi." / "managedinstance");
        # a plain Azure host is Azure SQL Database.
        if ".mi." in cs or "managedinstance" in cs or "managed-instance" in cs:
            return "azure_managed_instance"
        return "azuresql"
    if cs.startswith(("postgresql://", "postgres://")) or "psql odbc" in cs or "postgresql" in cs:
        return "postgres"
    if cs.startswith("mysql://") or "mysql" in cs:
        return "mysql"
    if cs.startswith("snowflake://") or "snowflakecomputing.com" in cs:
        return "snowflake"
    if cs.startswith(("oracle://", "oracle+")) or "oraclecloud.com" in cs:
        return "oracle"
    if (cs.startswith(("sqlite://", "file:"))
            or cs.endswith((".db", ".sqlite", ".sqlite3", ".db3"))):
        return "sqlite"
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
    "PostgresAdapter", "MySQLAdapter", "SqliteAdapter", "SnowflakeAdapter",
    "OracleAdapter", "AzureMiAdapter", "SynapseAdapter", "RedshiftAdapter",
    "DatabricksAdapter", "BigQueryAdapter", "CockroachDBAdapter", "Db2Adapter",
    "UnsupportedDialectError", "DIALECTS", "SUPPORTED_DIALECTS",
    "PLANNED_DIALECTS", "DIALECT_CHOICES", "detect_dialect", "get_adapter",
]
