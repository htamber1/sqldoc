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
        # Capacity queries carry marker comments; route them first because they
        # reuse tokens (dm_os_volume_stats / dm_db_index_physical_stats) that
        # match the server/health branches below.
        if "BASELINE_CONN" in sql:
            self._last = "baseline_conn"
        elif "BASELINE_QUERIES" in sql:
            self._last = "baseline_queries"
        elif "CAPACITY_SIZE" in sql:
            self._last = "cap_size"
        elif "CAPACITY_FRAG" in sql:
            self._last = "cap_frag"
        elif "CAPACITY_TABLES" in sql:
            self._last = "cap_tables"
        # `plans` joins dm_exec_query_stats + dm_exec_query_plan; check the plan
        # token first so it doesn't misroute to the health "slow" branch below.
        elif "dm_exec_query_plan" in sql:
            self._last = "mssql_plans"
        # Health DMV queries first — the dead-tables query aliases `p.rows AS
        # row_count`, which would otherwise misroute to the extractor branch.
        elif "dm_exec_query_stats" in sql:
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
        elif "version_store_reserved_page_count" in sql:
            self._last = "tempdb_vstore"
        elif "DB_ID('tempdb')" in sql:
            self._last = "tempdb_files"
        elif "dm_db_session_space_usage" in sql:
            self._last = "tempdb_sessions"
        elif "dm_os_waiting_tasks" in sql:
            self._last = "tempdb_contention"
        elif "fn_trace_gettable" in sql:
            self._last = "tempdb_autogrow"
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
        elif "dm_database_backups" in sql:
            self._last = "mi_backups"
        elif "dm_geo_replication_link_status" in sql:
            self._last = "mi_geo"
        elif "pdw_table_distribution_properties" in sql:
            self._last = "synapse_dist"
        elif "workload_management_workload_groups" in sql:
            self._last = "synapse_workload"
        elif "svv_table_info" in sql:
            self._last = "rs_tableinfo"
        elif "stv_wlm_service_class_config" in sql:
            self._last = "rs_wlm"
        elif "stl_alert_event_log" in sql:
            self._last = "rs_alerts"
        elif "DBX_TABLES" in sql:
            self._last = "dbx_tables"
        elif "DBX_COLUMNS" in sql:
            self._last = "dbx_columns"
        elif "DBX_PK" in sql:
            self._last = "dbx_pk"
        elif "DBX_VIEWS" in sql:
            self._last = "dbx_views"
        elif "DBX_ROUTINES" in sql:
            self._last = "dbx_routines"
        elif "DBX_HISTORY" in sql:
            self._last = "dbx_history"
        elif "DBX_DETAIL" in sql:
            self._last = "dbx_detail"
        elif "crdb_internal.zones" in sql:
            self._last = "crdb_zones"
        elif "crdb_internal.gossip_nodes" in sql:
            self._last = "crdb_nodes"
        elif "backupset" in sql:
            self._last = "backups"
        elif "'archive_mode'" in sql:
            self._last = "pgarchmode"
        elif "pg_stat_archiver" in sql:
            self._last = "pgarchiver"
        elif "pg_database" in sql:
            self._last = "pgdatabases"
        elif "@@log_bin" in sql:
            self._last = "mysqllogbin"
        elif "information_schema.schemata" in sql:
            self._last = "mysqlschemas"
        elif "sys.sql_logins" in sql:
            self._last = "mssql_logins"
        elif "sys.configurations" in sql:
            self._last = "mssql_config"
        elif "is_trustworthy_on" in sql:
            self._last = "mssql_trustworthy"
        elif "DATABASE_PRINCIPAL_ID('public')" in sql:
            self._last = "mssql_public"
        elif "rolsuper" in sql:
            self._last = "pg_superusers"
        elif "pg_hba_file_rules" in sql:
            self._last = "pg_hba"
        elif "has_schema_privilege" in sql:
            self._last = "pg_pubschema"
        elif "'ssl'" in sql:
            self._last = "pg_ssl"
        elif "mysql.user" in sql:
            self._last = "mysql_users"
        elif "user_privileges" in sql:
            self._last = "mysql_fileprivs"
        elif "@@secure_file_priv" in sql:
            self._last = "mysql_sfp"
        elif "dm_os_wait_stats" in sql:
            self._last = "mssql_waits"
        elif "wait_event_type" in sql:
            self._last = "pgwaits"
        elif "NOT granted" in sql:
            self._last = "pglocks"
        elif "events_waits_summary_global_by_event_name" in sql:
            self._last = "mysql_waits"
        elif "dm_hadr_availability_replica_states" in sql:
            self._last = "mssql_ha"
        elif "pg_stat_replication" in sql:
            self._last = "pg_repl"
        elif "pg_stat_wal_receiver" in sql:
            self._last = "pg_walrecv"
        elif "REPLICA STATUS" in sql or "SLAVE STATUS" in sql:
            self._last = "mysql_repl"
        elif "xml_deadlock_report" in sql:
            self._last = "mssql_deadlocks"
        elif "pg_blocking_pids" in sql:
            self._last = "pg_blocking"
        elif "pg_stat_database" in sql:
            self._last = "pg_deadlock_count"
        elif "events_errors_summary_global_by_error" in sql:
            self._last = "mysql_deadlocks"
        elif "pg_stat_statements" in sql:
            self._last = "pg_plans"
        elif "events_statements_summary_by_digest" in sql:
            self._last = "mysql_plans"
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


# A minimal SQL Server showplan XML with several anti-patterns.
_PLAN_XML = """<?xml version="1.0"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">
 <BatchSequence><Batch><Statements><StmtSimple>
  <QueryPlan>
   <MissingIndexes><MissingIndexGroup Impact="87.5"><MissingIndex/></MissingIndexGroup></MissingIndexes>
   <RelOp PhysicalOp="Table Scan" EstimateRows="500000"/>
   <RelOp PhysicalOp="Key Lookup" EstimateRows="1200"/>
   <RelOp PhysicalOp="Clustered Index Scan" EstimateRows="50000"/>
   <Warnings><SpillToTempDb SpillLevel="1"/><PlanAffectingConvert ConvertIssue="Seek Plan"/></Warnings>
  </QueryPlan>
 </StmtSimple></Statements></Batch></BatchSequence>
</ShowPlanXML>"""


@pytest.fixture
def fake_mssql_baseline_rows():
    """Rows the SQL Server baseline capture would see (connections + queries +
    one job; combine with fake_mssql_waits_rows for wait categories)."""
    return {
        "baseline_conn": [FakeRow(n=25)],
        "baseline_queries": [
            FakeRow(qid="0xAAAA", avg_ms=120.0, query_text="SELECT * FROM Sales.Orders WHERE Id=1"),
            FakeRow(qid="0xBBBB", avg_ms=40.0, query_text="SELECT COUNT(*) FROM Sales.Orders"),
        ],
        "agentjobs": [
            FakeRow(job_id="J1", job_name="Nightly ETL", enabled=1, owner="sa", category="c",
                    last_run_status=1, last_run_time="t", run_duration_seconds=200,
                    avg_duration_seconds=150, next_run_datetime="0"),
        ],
        "agentjobsteps": [],
    }


@pytest.fixture
def fake_mssql_capacity_rows():
    return {
        "cap_size": [FakeRow(db_mb=20480.0, max_mb=51200.0,
                             disk_free_mb=40960.0, disk_total_mb=204800.0)],
        "cap_frag": [FakeRow(frag=23.4)],
        "cap_tables": [
            FakeRow(obj="Sales.Orders", size_mb=8000.0, row_count=5000000),
            FakeRow(obj="Sales.OrderLines", size_mb=12000.0, row_count=40000000),
        ],
    }


@pytest.fixture
def fake_pg_capacity_rows():
    return {
        "cap_size": [FakeRow(db_mb=5120.0)],
        "cap_tables": [FakeRow(obj="public.film", size_mb=200.0, row_count=1000)],
    }


@pytest.fixture
def fake_mssql_plans_rows():
    return {"mssql_plans": [
        FakeRow(avg_elapsed_ms=1200.5, execution_count=340, total_elapsed_ms=408170.0,
                avg_reads=90000, query_text="SELECT * FROM Sales.Orders WHERE CustomerId = 5",
                plan_xml=_PLAN_XML),
        FakeRow(avg_elapsed_ms=80.0, execution_count=10, total_elapsed_ms=800.0,
                avg_reads=50, query_text="SELECT COUNT(*) FROM Sales.Orders",
                plan_xml="<ShowPlanXML/>"),
    ]}


@pytest.fixture
def fake_pg_plans_rows():
    return {"pg_plans": [
        FakeRow(query="SELECT * FROM film WHERE title = $1", calls=500,
                total_ms=45000.0, avg_ms=90.0, avg_reads=12000),
    ]}


@pytest.fixture
def fake_mysql_plans_rows():
    return {"mysql_plans": [
        FakeRow(query="SELECT * FROM rental WHERE customer_id = ?", calls=8000,
                total_ms=60000.0, avg_ms=7.5, rows_examined=4000000, no_index_used=8000),
    ]}


_DEADLOCK_XML = """<RingBufferTarget>
  <event name="xml_deadlock_report" timestamp="2026-07-11T10:00:00.000Z">
    <data name="xml_report"><value>
      <deadlock>
        <victim-list><victimProcess id="process1"/></victim-list>
        <process-list>
          <process id="process1" spid="55" currentdb="5" lockMode="X" waitresource="KEY: 5:72" loginname="app" hostname="web1">
            <inputbuf>UPDATE Orders SET Total=1 WHERE Id=10</inputbuf>
          </process>
          <process id="process2" spid="60" currentdb="5" lockMode="X" waitresource="KEY: 5:73" loginname="app" hostname="web2">
            <inputbuf>UPDATE Orders SET Total=2 WHERE Id=11</inputbuf>
          </process>
        </process-list>
        <resource-list>
          <keylock dbid="5" objectname="db.dbo.Orders" indexname="PK" mode="X">
            <owner-list><owner id="process2" mode="X"/></owner-list>
            <waiter-list><waiter id="process1" mode="X"/></waiter-list>
          </keylock>
          <keylock dbid="5" objectname="db.dbo.Orders" indexname="PK" mode="X">
            <owner-list><owner id="process1" mode="X"/></owner-list>
            <waiter-list><waiter id="process2" mode="X"/></waiter-list>
          </keylock>
        </resource-list>
      </deadlock>
    </value></data>
  </event>
</RingBufferTarget>"""


@pytest.fixture
def fake_mssql_deadlock_rows():
    return {"mssql_deadlocks": [FakeRow(target_xml=_DEADLOCK_XML)]}


@pytest.fixture
def fake_pg_deadlock_rows():
    return {
        "pg_deadlock_count": [FakeRow(total_deadlocks=7)],
        "pg_blocking": [FakeRow(blocked_pid=100, blocked_user="app",
                                blocked_query="UPDATE t SET x=1 WHERE id=5",
                                blocking_pid=101, blocking_user="app",
                                blocking_query="UPDATE t SET x=2 WHERE id=6")],
    }


@pytest.fixture
def fake_mysql_deadlock_rows():
    return {"mysql_deadlocks": [FakeRow(n=3)]}


@pytest.fixture
def fake_mssql_ha_rows():
    return {
        "mssql_ha": [
            FakeRow(ag_name="AG1", replica_server_name="SQLNODE1", role_desc="PRIMARY",
                    operational_state_desc="ONLINE", synchronization_health_desc="HEALTHY",
                    connected_state_desc="CONNECTED", log_send_queue_kb=0, redo_queue_kb=0,
                    sync_state="SYNCHRONIZED"),
            FakeRow(ag_name="AG1", replica_server_name="SQLNODE2", role_desc="SECONDARY",
                    operational_state_desc="ONLINE", synchronization_health_desc="HEALTHY",
                    connected_state_desc="CONNECTED", log_send_queue_kb=100, redo_queue_kb=50,
                    sync_state="SYNCHRONIZED"),
            FakeRow(ag_name="AG1", replica_server_name="SQLNODE3", role_desc="SECONDARY",
                    operational_state_desc="", synchronization_health_desc="NOT_HEALTHY",
                    connected_state_desc="DISCONNECTED", log_send_queue_kb=5000, redo_queue_kb=2000,
                    sync_state="NOT SYNCHRONIZING"),
        ],
    }


@pytest.fixture
def fake_pg_ha_rows():
    return {
        "pg_repl": [
            FakeRow(application_name="standby1", client_addr="10.0.0.2", state="streaming",
                    sync_state="sync", replay_lag_seconds=0.5, lag_bytes=1024),
            FakeRow(application_name="standby2", client_addr="10.0.0.3", state="streaming",
                    sync_state="async", replay_lag_seconds=120.0, lag_bytes=5000000),   # lagging
        ],
        "pg_walrecv": [],
    }


@pytest.fixture
def fake_mysql_ha_rows():
    return {
        "mysql_repl": [
            FakeRow(Source_Host="10.0.0.1", Replica_IO_Running="Yes", Replica_SQL_Running="Yes",
                    Seconds_Behind_Source=45),                # healthy but lagging 45s
        ],
    }


@pytest.fixture
def fake_mssql_waits_rows():
    return {
        "mssql_waits": [
            FakeRow(wait_type="PAGEIOLATCH_SH", wait_time_ms=50000, waiting_tasks_count=800),    # IO
            FakeRow(wait_type="LCK_M_X", wait_time_ms=30000, waiting_tasks_count=120),            # Lock
            FakeRow(wait_type="SOS_SCHEDULER_YIELD", wait_time_ms=15000, waiting_tasks_count=9000),  # CPU
            FakeRow(wait_type="RESOURCE_SEMAPHORE", wait_time_ms=4000, waiting_tasks_count=20),   # Memory
            FakeRow(wait_type="ASYNC_NETWORK_IO", wait_time_ms=1000, waiting_tasks_count=300),    # Network
        ],
    }


@pytest.fixture
def fake_pg_waits_rows():
    return {
        "pgwaits": [
            FakeRow(wait_event_type="IO", wait_event="DataFileRead", n=5),
            FakeRow(wait_event_type="Lock", wait_event="relation", n=3),
            FakeRow(wait_event_type="Client", wait_event="ClientRead", n=2),
        ],
        "pglocks": [FakeRow(blocked=2)],
    }


@pytest.fixture
def fake_mysql_waits_rows():
    return {
        "mysql_waits": [
            FakeRow(event_name="wait/io/file/innodb/innodb_data_file",
                    count_star=10000, sum_timer_wait=60_000_000_000_000),   # 60000 ms, IO
            FakeRow(event_name="wait/lock/table/sql/handler",
                    count_star=500, sum_timer_wait=20_000_000_000_000),     # 20000 ms, Lock
            FakeRow(event_name="wait/synch/mutex/innodb/log_sys_mutex",
                    count_star=3000, sum_timer_wait=5_000_000_000_000),     # 5000 ms, CPU
        ],
    }


@pytest.fixture
def fake_mssql_secure_rows():
    return {
        "mssql_logins": [
            FakeRow(name="sa", is_disabled=0, blank_pw=0),        # SA enabled -> MEDIUM
            FakeRow(name="app", is_disabled=0, blank_pw=1),       # blank password -> HIGH
        ],
        "mssql_config": [
            FakeRow(name="xp_cmdshell", v=1),                     # HIGH
            FakeRow(name="clr enabled", v=0),
        ],
        "mssql_trustworthy": [FakeRow(name="Payments")],          # HIGH
        "mssql_public": [FakeRow(n=3)],                           # MEDIUM
    }


@pytest.fixture
def fake_pg_secure_rows():
    return {
        "pg_superusers": [FakeRow(rolname="postgres"), FakeRow(rolname="admin")],  # admin MEDIUM, postgres LOW
        "pg_hba": [
            FakeRow(type="host", database="all", user_name="all",
                    address="0.0.0.0/0", auth_method="trust"),    # HIGH
            FakeRow(type="host", database="all", user_name="all",
                    address="10.0.0.0/8", auth_method="password"),  # MEDIUM
        ],
        "pg_pubschema": [FakeRow(pub_create=True)],               # MEDIUM
        "pg_ssl": [FakeRow(setting="off")],                       # MEDIUM
    }


@pytest.fixture
def fake_mysql_secure_rows():
    return {
        "mysql_users": [
            FakeRow(user="", host="localhost", authentication_string="x",
                    plugin="mysql_native_password"),              # anonymous -> HIGH
            FakeRow(user="root", host="%", authentication_string="hash",
                    plugin="mysql_native_password"),              # remote root -> HIGH
            FakeRow(user="app", host="%", authentication_string="",
                    plugin="mysql_native_password"),              # no password -> HIGH
        ],
        "mysql_fileprivs": [FakeRow(grantee="'app'@'%'")],        # non-root FILE -> MEDIUM
        "mysql_sfp": [FakeRow(sfp=None)],                         # unrestricted -> MEDIUM
    }


@pytest.fixture
def fake_backup_rows():
    """Rows the SQL Server backup query (msdb.dbo.backupset) would return."""
    return {
        "backups": [
            FakeRow(database_name="AdventureWorks2022", recovery_model_desc="FULL",
                    last_full="2026-07-11 02:00:00", last_diff=None, last_log=None,
                    full_age_hours=6),                       # FULL but no log backup -> issue
            FakeRow(database_name="Sales", recovery_model_desc="SIMPLE",
                    last_full="2026-07-10 02:00:00", last_diff=None, last_log=None,
                    full_age_hours=30),                      # SIMPLE -> no PITR issue
            FakeRow(database_name="Staging", recovery_model_desc="FULL",
                    last_full=None, last_diff=None, last_log=None,
                    full_age_hours=None),                    # never backed up
        ],
    }


@pytest.fixture
def fake_pg_backup_rows():
    return {
        "pgarchmode": [FakeRow(setting="on")],
        "pgarchiver": [FakeRow(last_archived_time="2026-07-11 05:00:00",
                               last_archived_wal="000000010000000000000042",
                               archived_count=1200, failed_count=0,
                               last_failed_time=None, age_hours=2.5)],
        "pgdatabases": [FakeRow(datname="pagila"), FakeRow(datname="analytics")],
    }


@pytest.fixture
def fake_mysql_backup_rows():
    return {
        "mysqllogbin": [FakeRow(log_bin=1, basename="/var/lib/mysql/binlog")],
        "mysqlschemas": [FakeRow(schema_name="sakila"), FakeRow(schema_name="app")],
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
        "tempdb_vstore": [FakeRow(version_store_mb=512.0, version_gen_kb=1024, version_cleanup_kb=1000)],
        "tempdb_files": [FakeRow(file_count=2, data_files=1, total_size_mb=8192.0, cpu_count=8)],  # 1 file < 8
        "tempdb_sessions": [FakeRow(session_id=70, user_mb=120.0, internal_mb=45.0, login_name="app")],
        "tempdb_contention": [FakeRow(contention=3)],
        "tempdb_autogrow": [FakeRow(growth_events=5)],
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
