"""Azure SQL Managed Instance adapter.

Managed Instance speaks the same T-SQL as boxed SQL Server, so the metadata
extraction is inherited unchanged from :class:`SqlServerAdapter`. What differs is
the *operational* surface, which the feature modules handle by dialect:

* Some server-scoped DMVs are restricted on MI (they degrade to a note via the
  usual per-check try/except).
* SQL Agent works, but backups are **managed by Azure** — the ``backup`` module
  shows the Azure automated-backup status via ``sys.dm_database_backups`` for
  this dialect instead of ``msdb.dbo.backupset``.
* High availability is **built in** — the ``ha`` module shows geo-replication
  link status (``sys.dm_geo_replication_link_status``) for this dialect.

Detected from a ``*.database.windows.net`` host that carries an MI marker
(``.mi.`` / ``managedinstance``); otherwise a ``database.windows.net`` host is
treated as Azure SQL Database (``azuresql``).

NOTE: mock-tested only — not run against a live Managed Instance.
"""
from sqldoc.adapters.base import Capabilities
from sqldoc.adapters.sqlserver import SqlServerAdapter


class AzureMiAdapter(SqlServerAdapter):
    dialect = "azure_managed_instance"
    display_name = "Azure SQL Managed Instance"
    # MI supports the full DMV/Agent surface (with a few restricted DMVs that
    # degrade per-check), plus Azure-managed backup + built-in HA.
    capabilities = Capabilities(quality=True, health=True, access_audit=True,
                                server_monitoring=True, infra_monitoring=True)
