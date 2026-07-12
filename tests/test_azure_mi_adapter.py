"""Azure SQL Managed Instance: detection, capabilities, MI backup + geo HA."""
from sqldoc.adapters import get_adapter, detect_dialect, DIALECTS
from sqldoc.adapters.azure_mi import AzureMiAdapter
from sqldoc.backup import collect_backups
from sqldoc.ha import collect_ha
from conftest import FakeConnection, FakeAdapter, FakeRow


# --- detection + registration -----------------------------------------------

def test_detects_managed_instance():
    assert detect_dialect("Server=myinst.abc123.mi.database.windows.net;Database=x") == "azure_managed_instance"
    assert detect_dialect("Server=managedinstance01.database.windows.net;Database=x") == "azure_managed_instance"
    # a plain Azure SQL DB host stays azuresql
    assert detect_dialect("Server=myserver.database.windows.net;Database=x") == "azuresql"


def test_registered_and_capabilities():
    assert DIALECTS["azure_managed_instance"] is AzureMiAdapter
    a = get_adapter("Server=x.mi.database.windows.net;", "azure_managed_instance")
    assert isinstance(a, AzureMiAdapter)
    assert a.dialect == "azure_managed_instance"
    assert a.display_name == "Azure SQL Managed Instance"
    assert a.capabilities.server_monitoring and a.capabilities.infra_monitoring


def test_inherits_sqlserver_extraction(fake_table_rows):
    # MI uses the same T-SQL extraction as SQL Server.
    a = AzureMiAdapter("Server=x.mi.database.windows.net;",
                       connect=lambda cs: FakeConnection(fake_table_rows))
    tables = a.extract_metadata()
    assert tables and tables[0].name == "Orders"


# --- MI-managed backup ------------------------------------------------------

def test_mi_azure_managed_backup():
    rows = {"mi_backups": [
        FakeRow(database_name="Sales", last_full="2026-07-12 02:00:00", last_diff=None,
                last_log="2026-07-12 09:00:00", full_age_hours=7),
        FakeRow(database_name="NewDb", last_full=None, last_diff=None, last_log=None,
                full_age_hours=None),
    ]}
    report = collect_backups(FakeAdapter(FakeConnection(rows), dialect="azure_managed_instance"))
    assert report.pitr_enabled and report.pitr_mechanism == "Azure automated backups"
    dbs = {d.database: d for d in report.databases}
    assert dbs["Sales"].pitr_capable and dbs["Sales"].last_log_backup
    assert dbs["NewDb"].never_backed_up
    assert any("managed automatically by Azure" in n for n in report.notes)


# --- MI geo-replication HA --------------------------------------------------

def test_mi_geo_replication_ha():
    rows = {"mi_geo": [
        FakeRow(partner_server="eastus2-mi", partner_database="Sales", role_desc="SECONDARY",
                replication_state_desc="CATCH_UP", secondary_allow_connections_desc="ALL",
                last_replication="2026-07-12 09:00:00", replication_lag_sec=3),
    ]}
    report = collect_ha(FakeAdapter(FakeConnection(rows), dialect="azure_managed_instance"))
    assert report.ha_enabled and report.mechanism == "Azure geo-replication"
    r = report.replicas[0]
    assert r.server == "eastus2-mi" and r.lag_seconds == 3.0
    assert r.is_healthy                    # CATCH_UP is the healthy geo steady state


def test_mi_geo_none():
    report = collect_ha(FakeAdapter(FakeConnection({}), dialect="azure_managed_instance"))
    assert not report.ha_enabled and report.notes
