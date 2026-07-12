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


class FakeAdapter:
    """Minimal adapter stand-in for health/quality unit tests: a fixed dialect
    and a pre-built (fake) connection."""
    def __init__(self, conn, dialect="sqlserver", display_name=None, capabilities=None):
        from sqldoc.adapters.base import Capabilities
        self._conn = conn
        self.dialect = dialect
        self.display_name = display_name or dialect
        self.capabilities = capabilities or Capabilities(
            quality=True, health=True, access_audit=True)

    def connect(self):
        return self._conn

    def cursor(self, conn):
        return conn.cursor()


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
        # Support both positional (pyodbc-style) and string-key (dict-cursor)
        # access, so one fake row mimics pyodbc rows and MySQL dict cursors.
        if isinstance(i, str):
            return self._d[i]
        return list(self._d.values())[i]

    def get(self, k, default=None):
        return self._d.get(k, default)


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
        elif "dm_exec_procedure_stats" in sql or "pg_stat_user_functions" in sql:
            self._last = "unusedprocs"
        elif "RING_BUFFER_SCHEDULER_MONITOR" in sql:
            self._last = "srv_cpu"
        elif "dm_os_memory_clerks" in sql:
            self._last = "srv_mem"
        elif "dm_os_volume_stats" in sql:
            self._last = "srv_vol"
        elif "dm_io_virtual_file_stats" in sql:
            self._last = "srv_io"
        elif "dm_exec_requests" in sql:
            self._last = "srv_req"
        elif "dm_os_sys_info" in sql:
            self._last = "srv_info"
        elif "dm_exec_sessions" in sql:
            self._last = "srv_sess"
        elif "sysjobschedules" in sql:
            self._last = "agentjobs"
        elif "sysjobhistory" in sql:
            self._last = "agentjobsteps"
        elif "xp_readerrorlog" in sql:
            self._last = "errorlog"
        elif "@@SERVERNAME" in sql:
            self._last = "localname"
        elif "linked_logins" in sql:
            self._last = "linkedlogins"
        elif "sp_testlinkedserver" in sql:
            self._last = "linkedtest"
        elif "OPENQUERY" in sql:
            self._last = "linkedprobe"
        elif "sys.servers" in sql:
            self._last = "linkedservers"
        elif "database_role_members" in sql:
            self._last = "rolemembers"
        elif "pg_auth_members" in sql:
            self._last = "pgrolemembers"
        elif "database_permissions" in sql:
            self._last = "perms"
        elif "table_privileges" in sql:
            self._last = "pgperms"
        elif "dup_rows" in sql:
            self._last = "qdup"
        elif "non_null" in sql:
            self._last = "qstats"
        elif " AS freq" in sql:
            self._last = "qtop"
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
def fake_permission_rows():
    """Rows sys.database_permissions would return (object-level grants)."""
    return {
        "perms": [
            FakeRow(principal_name="app_reader", principal_type="SQL_USER",
                    permission_name="SELECT", state_desc="GRANT",
                    schema_name="dbo", object_name="People", object_type="USER_TABLE"),
            FakeRow(principal_name="analyst", principal_type="SQL_USER",
                    permission_name="SELECT", state_desc="DENY",
                    schema_name="dbo", object_name="People", object_type="USER_TABLE"),
            FakeRow(principal_name="app_reader", principal_type="SQL_USER",
                    permission_name="SELECT", state_desc="GRANT",
                    schema_name="dbo", object_name="Products", object_type="USER_TABLE"),
        ],
    }


@pytest.fixture
def fake_role_member_rows():
    """Rows sys.database_role_members would return (SQL Server role membership)."""
    return {
        "rolemembers": [
            FakeRow(role_name="db_datareader", member_name="app_reader", member_type="SQL_USER"),
            FakeRow(role_name="db_datareader", member_name="analyst", member_type="SQL_USER"),
            FakeRow(role_name="db_owner", member_name="dba", member_type="SQL_USER"),
        ],
    }


@pytest.fixture
def fake_pg_grant_rows():
    """Rows information_schema.table_privileges returns (PostgreSQL/MySQL)."""
    return {
        "pgperms": [
            FakeRow(grantee="app_reader", table_schema="public",
                    table_name="people", privilege_type="SELECT"),
            FakeRow(grantee="postgres", table_schema="public",
                    table_name="people", privilege_type="INSERT"),
        ],
    }


@pytest.fixture
def fake_quality_rows():
    """Rows the quality aggregate queries would see (same stats for every
    column, which is fine for exercising the pipeline)."""
    return {
        "qstats": [FakeRow(total=100, non_null=40, distinct_count=1, blank_count=5,
                           min_val="0", max_val="9")],       # null_rate 0.6, constant, blanks
        "qtop": [FakeRow(val="0", freq=40)],
        "qdup": [FakeRow(dup_rows=8, dup_groups=3)],          # 5 redundant rows
    }


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
        "unusedprocs": [
            FakeRow(schema_name="Sales", procedure_name="uspLegacyExport",
                    execution_count=0, last_execution_time=None,
                    create_date="2021-01-01", modify_date="2021-01-01"),
        ],
    }


@pytest.fixture
def fake_linked_rows():
    """Rows the linked-server discovery queries would return."""
    return {
        "localname": [FakeRow(server_name="PRODSQL01")],
        "linkedservers": [
            FakeRow(name="REPORTING01", product="SQL Server", provider="SQLNCLI11",
                    data_source="reporting.corp", catalog="Reports",
                    is_rpc_out_enabled=1, is_data_access_enabled=1, is_remote_login_enabled=0),
            FakeRow(name="LEGACY_ORA", product="Oracle", provider="OraOLEDB.Oracle",
                    data_source="ORCL", catalog=None,
                    is_rpc_out_enabled=0, is_data_access_enabled=1, is_remote_login_enabled=0),
        ],
        "linkedlogins": [
            FakeRow(linked_server="REPORTING01", local_login="(all logins)",
                    remote_name="rpt_reader", uses_self_credential=0),
            FakeRow(linked_server="LEGACY_ORA", local_login="sa",
                    remote_name="system", uses_self_credential=0),
        ],
        "linkedtest": [],
        "linkedprobe": [FakeRow(product_version="15.0.4123.1", edition="Enterprise Edition")],
    }


@pytest.fixture
def fake_errorlog_rows():
    """Rows sys.xp_readerrorlog would return (LogDate/ProcessInfo/Text)."""
    return {
        "errorlog": [
            FakeRow(LogDate="2026-07-11 03:15:00", ProcessInfo="spid51",
                    Text="Error: 823, Severity: 24, State: 2. The operating system returned error to SQL Server during a read at offset. (corruption)"),
            FakeRow(LogDate="2026-07-11 04:00:00", ProcessInfo="spid12",
                    Text="Transaction (Process ID 62) was deadlocked on lock resources with another process and has been chosen as the deadlock victim."),
            FakeRow(LogDate="2026-07-11 05:30:00", ProcessInfo="Logon",
                    Text="Login failed for user 'sa'. Reason: Password did not match. [CLIENT: 10.0.0.5] Error: 18456, Severity: 14, State: 8."),
            FakeRow(LogDate="2026-07-11 06:00:00", ProcessInfo="spid7s",
                    Text="Server resumed execution after being idle. This is an informational message only. Severity: 10."),
        ],
    }


@pytest.fixture
def fake_server_rows():
    """Rows the instance-level server DMV queries would see."""
    return {
        "srv_info": [FakeRow(cpu_count=8, scheduler_count=8, hyperthread_ratio=2,
                             physical_memory_mb=32768,
                             sqlserver_start_time="2026-07-01 00:00:00",
                             uptime_seconds=864000)],           # 10 days
        "srv_cpu": [FakeRow(sql_cpu=35, idle_cpu=55, other_cpu=10, record_id=12345)],
        "srv_mem": [
            FakeRow(clerk_type="MEMORYCLERK_SQLBUFFERPOOL", mb=20480.0),
            FakeRow(clerk_type="CACHESTORE_SQLCP", mb=2048.0),
            FakeRow(clerk_type="OBJECTSTORE_LOCK_MANAGER", mb=512.0),
        ],
        "srv_vol": [
            FakeRow(volume_mount_point="C:\\", logical_volume_name="OS",
                    total_gb=200.0, available_gb=60.0, drive="C"),  # 30% free: OK
            FakeRow(volume_mount_point="D:\\", logical_volume_name="Data",
                    total_gb=500.0, available_gb=20.0, drive="D"),  # 4% free: LOW
        ],
        "srv_io": [
            FakeRow(drive="C", read_latency_ms=5.0, write_latency_ms=3.0),
            FakeRow(drive="D", read_latency_ms=12.0, write_latency_ms=8.0),
        ],
        "srv_req": [
            FakeRow(session_id=55, login_name="app", host_name="web1",
                    database_name="Sales", status="running", command="SELECT",
                    blocking_session_id=0, wait_type=None, cpu_ms=1200,
                    elapsed_ms=3400, reads=900, query_text="SELECT * FROM Orders"),
            FakeRow(session_id=60, login_name="rep", host_name="web2",
                    database_name="Sales", status="suspended", command="SELECT",
                    blocking_session_id=55, wait_type="LCK_M_S", cpu_ms=10,
                    elapsed_ms=5000, reads=3, query_text="SELECT * FROM Orders WHERE Id=1"),
        ],
        "srv_sess": [
            FakeRow(login_name="app", database_name="Sales", status="running"),
            FakeRow(login_name="app", database_name="Sales", status="sleeping"),
            FakeRow(login_name="rep", database_name="HR", status="running"),
        ],
        "agentjobs": [
            FakeRow(job_id="J1", job_name="Nightly ETL", enabled=1, owner="sa",
                    category="Data load", last_run_status=0,               # Failed
                    last_run_time="2026-07-11 02:00:00", run_duration_seconds=3600,
                    avg_duration_seconds=1200, next_run_datetime="2026-07-12 02:00:00"),
            FakeRow(job_id="J2", job_name="Backup Full", enabled=1, owner="sa",
                    category="Backup", last_run_status=1,                  # Succeeded
                    last_run_time="2026-07-11 01:00:00", run_duration_seconds=300,
                    avg_duration_seconds=300, next_run_datetime="2026-07-12 01:00:00"),
            FakeRow(job_id="J3", job_name="Old Cleanup", enabled=0, owner="sa",
                    category="Maintenance", last_run_status=1,             # Succeeded, disabled
                    last_run_time="2026-06-01 03:00:00", run_duration_seconds=60,
                    avg_duration_seconds=60, next_run_datetime=None),
        ],
        "agentjobsteps": [
            FakeRow(job_name="Nightly ETL", step_id=2, step_name="Load facts",
                    message="Cannot insert duplicate key row in object 'dbo.Fact'."),
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
