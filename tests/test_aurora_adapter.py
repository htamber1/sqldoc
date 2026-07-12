"""Amazon Aurora adapters: detection + replica-lag metrics (PG + MySQL)."""
from sqldoc.adapters import detect_dialect, get_adapter, DIALECTS
from sqldoc.adapters.aurora import AuroraMySQLAdapter, AuroraPostgresAdapter
from sqldoc.adapters.postgres import PostgresAdapter
from sqldoc.adapters.mysql import MySQLAdapter
from conftest import FakeRow


# --- a fake connection whose cursor accepts kwargs (MySQL uses dictionary=True)

class _Cursor:
    def __init__(self, data):
        self._data, self._key = data, None

    def execute(self, sql, *a, **k):
        if "replica_host_status" in sql:
            self._key = "mysql_lag"
        elif "aurora_replica_status" in sql:
            self._key = "pg_lag"
        else:
            self._key = "unknown"
        return self

    def fetchall(self):
        return self._data.get(self._key, [])


class _Conn:
    def __init__(self, data):
        self._data = data

    def cursor(self, *a, **k):
        return _Cursor(self._data)

    def close(self):
        pass


# --- detection --------------------------------------------------------------

def test_detection():
    assert detect_dialect(
        "postgresql://u:p@aurora-pg.cluster-abc.us-east-1.rds.amazonaws.com:5432/db") == "aurora_postgres"
    assert detect_dialect(
        "mysql://u:p@aurora-mysql.cluster-abc.us-east-1.rds.amazonaws.com:3306/db") == "aurora_mysql"
    # a non-Aurora RDS instance stays plain postgres/mysql
    assert detect_dialect("postgresql://u:p@mydb.abc.us-east-1.rds.amazonaws.com/db") == "postgres"
    assert DIALECTS["aurora_postgres"] is AuroraPostgresAdapter
    assert DIALECTS["aurora_mysql"] is AuroraMySQLAdapter
    assert issubclass(AuroraPostgresAdapter, PostgresAdapter)
    assert issubclass(AuroraMySQLAdapter, MySQLAdapter)


# --- replica lag ------------------------------------------------------------

def test_aurora_mysql_replica_lag():
    data = {"mysql_lag": [
        FakeRow(SERVER_ID="writer-1", SESSION_ID="MASTER_SESSION_ID", lag_ms=0, CPU=12.5),
        FakeRow(SERVER_ID="reader-1", SESSION_ID="abc-123", lag_ms=45, CPU=8.0),
    ]}
    a = AuroraMySQLAdapter("mysql://u:p@x.cluster-y.rds.amazonaws.com/db?aurora",
                           connect=lambda cs: _Conn(data))
    lag = a.aurora_replica_lag()
    writer = next(x for x in lag if x["role"] == "WRITER")
    reader = next(x for x in lag if x["role"] == "READER")
    assert writer["server_id"] == "writer-1" and writer["lag_ms"] == 0.0
    assert reader["lag_ms"] == 45.0 and reader["cpu"] == 8.0


def test_aurora_postgres_replica_lag():
    data = {"pg_lag": [
        FakeRow(server_id="pg-reader-1", lag_ms=20, cpu=5.0, is_current=True),
    ]}
    a = AuroraPostgresAdapter("postgresql://u:p@x.cluster-y.rds.amazonaws.com:5432/db?aurora",
                              connect=lambda cs: _Conn(data))
    lag = a.aurora_replica_lag()
    assert lag[0]["server_id"] == "pg-reader-1" and lag[0]["lag_ms"] == 20.0


def test_registered_via_get_adapter():
    a = get_adapter("mysql://u:p@aurora.cluster-x.rds.amazonaws.com/db", "aurora_mysql")
    assert a.dialect == "aurora_mysql"
