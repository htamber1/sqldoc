"""Orchestrate the access check across the configured servers + databases.

Resolves the AD user once, then for each database: extracts the schema (for the
PII cross-reference), reads the catalog, matches the user's logins/groups, and
computes effective access. Best-effort — a database that can't be reached is
recorded as a note, not a failure.
"""
import re

from sqldoc.access import ad as ad_mod
from sqldoc.access import config as access_config
from sqldoc.access.model import AccessReport
from sqldoc.access.sqlserver import (
    collect_server_logins, match_user_logins, collect_db_access)


def _with_database(conn_str: str, database: str) -> str:
    """Point an ODBC/connection string at a specific database."""
    if not conn_str:
        return conn_str
    if re.search(r'(?i)(DATABASE|Initial\s+Catalog)\s*=', conn_str):
        return re.sub(r'(?i)(DATABASE|Initial\s+Catalog)\s*=[^;]*',
                      f'DATABASE={database}', conn_str, count=1)
    return conn_str.rstrip(';') + f';DATABASE={database}'


def build_db_adapter(server_entry: dict, database: str):
    """A DatabaseAdapter pointed at one database on one server."""
    from sqldoc.adapters import get_adapter
    from sqldoc.extractor import build_connection_string
    dialect = server_entry.get("dialect", "sqlserver")
    conn_str = server_entry.get("connection_string")
    if conn_str:
        conn_str = _with_database(conn_str, database)
    else:
        conn_str = build_connection_string(
            server_entry.get("server"), database,
            server_entry.get("username"), server_entry.get("password"))
    return get_adapter(conn_str, dialect)


def check_access(cfg: dict, identifier: str, source=None, adapter_factory=None) -> AccessReport:
    """Build an AccessReport for `identifier` across the configured servers."""
    source = source or ad_mod.get_source(access_config.ad_config(cfg))
    user = source.get_user(identifier)
    report = AccessReport(user=user)
    if not user.found:
        report.errors.append(("ad", f"User '{identifier}' not found in {user.source or 'AD'}."))
        return report

    factory = adapter_factory or build_db_adapter
    matched_group_names = set()

    for entry in access_config.servers(cfg):
        server_name = entry["name"]
        for database in entry["databases"]:
            try:
                adapter = factory(entry, database)
                tables = adapter.extract_metadata()
                from sqldoc.pii import scan_tables
                pii = scan_tables(tables)
                conn = adapter.connect()
                try:
                    cursor = adapter.cursor(conn)
                    logins = collect_server_logins(cursor)
                    matched = match_user_logins(logins, user)
                    for lg in matched:
                        lg.server = server_name
                        if lg not in report.logins:
                            report.logins.append(lg)
                        if "GROUP" in (lg.type or "").upper():
                            matched_group_names.add(lg.name)
                    access = collect_db_access(cursor, server_name, database, matched, pii)
                    report.access.extend(access)
                finally:
                    conn.close()
            except Exception as e:
                report.errors.append(
                    (f"{server_name}/{database}", f"{type(e).__name__}: {e}"))

    report.matched_groups = sorted(matched_group_names)
    return report
