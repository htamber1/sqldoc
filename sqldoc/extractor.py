"""Backward-compatible facade over the adapter layer.

The extraction logic now lives in `sqldoc.adapters` (dialect-neutral dataclasses
in `adapters.base`, SQL Server T-SQL in `adapters.sqlserver`). This module keeps
the historical surface — the shared dataclasses plus `build_connection_string`,
`get_connection`, and the `extract_*` free functions — so existing imports
(`from sqldoc.extractor import Table`, etc.) and the SQL-Server code paths work
unchanged.

`get_connection` stays a real module-level function on purpose: the analysis
modules (`health`, `quality`, `comply`, `pii`) import it here and tests
monkeypatch it, so it must remain the connection seam. The `extract_*` wrappers
resolve `get_connection` from this module's namespace at call time and inject it
into the adapter, so patching `extractor.get_connection` transparently redirects
the adapter's connection too.
"""
import pyodbc

from sqldoc.adapters.base import (
    Column, Index, Trigger, CheckConstraint, UniqueConstraint,
    Table, View, Parameter, StoredProcedure,
)
from sqldoc.adapters.sqlserver import SqlServerAdapter

__all__ = [
    "Column", "Index", "Trigger", "CheckConstraint", "UniqueConstraint",
    "Table", "View", "Parameter", "StoredProcedure",
    "build_connection_string", "get_connection",
    "extract_metadata", "extract_views", "extract_procedures",
]


def build_connection_string(server: str, database: str, username: str, password: str,
                            windows_auth: bool = False, driver: str = None) -> str:
    return SqlServerAdapter.build_connection_string(
        server, database, username, password,
        windows_auth=windows_auth, driver=driver)


def get_connection(connection_string: str):
    return pyodbc.connect(connection_string)


def _adapter(connection_string: str) -> SqlServerAdapter:
    # Inject this module's get_connection (looked up now, so a monkeypatch on
    # extractor.get_connection is honored) as the adapter's connector.
    return SqlServerAdapter(connection_string, connect=get_connection)


def extract_metadata(connection_string: str) -> list[Table]:
    return _adapter(connection_string).extract_metadata()


def extract_views(connection_string: str) -> list[View]:
    return _adapter(connection_string).extract_views()


def extract_procedures(connection_string: str) -> list[StoredProcedure]:
    return _adapter(connection_string).extract_procedures()
