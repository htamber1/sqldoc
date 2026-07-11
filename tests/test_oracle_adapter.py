"""OracleAdapter with a fake cursor that emulates oracledb (description + tuple
rows), mock-only — no live Oracle instance."""
import builtins
import pytest

from sqldoc.adapters.oracle import OracleAdapter, _trigger_events


def _route(sql):
    s = sql
    if "all_tab_columns" in s:
        return "columns"
    if "all_tables" in s:
        return "tables"
    if "constraint_type = 'P'" in s:
        return "pk"
    if "constraint_type = 'R'" in s:
        return "fk"
    if "constraint_type = 'U'" in s:
        return "uniques"
    if "constraint_type = 'C'" in s:
        return "checks"
    if "all_indexes" in s:
        return "indexes"
    if "all_triggers" in s:
        return "triggers"
    if "all_views" in s:
        return "views"
    if "all_procedures" in s:
        return "procs"
    if "all_arguments" in s:
        return "params"
    return "unknown"


class _OraCursor:
    def __init__(self, data):
        self._data = data
        self._rows = []
        self._desc = []

    def execute(self, sql, params=None):
        rows = self._data.get(_route(sql), [])
        self._rows = rows
        keys = list(rows[0].keys()) if rows else []
        self._desc = [(k.upper(),) for k in keys]   # Oracle upper-cases names
        return self

    @property
    def description(self):
        return self._desc

    def fetchall(self):
        cols = [d[0].lower() for d in self._desc]
        return [tuple(r.get(c) for c in cols) for r in self._rows]


class _OraConn:
    def __init__(self, data):
        self._data = data

    def cursor(self):
        return _OraCursor(self._data)

    def close(self):
        pass


@pytest.fixture
def ora_rows():
    return {
        "tables": [{"table_name": "ORDERS", "num_rows": 1500}],
        "columns": [
            {"column_name": "ORDER_ID", "data_type": "NUMBER", "data_length": 22,
             "nullable": "N", "data_default": None},
            {"column_name": "CUSTOMER_ID", "data_type": "NUMBER", "data_length": 22,
             "nullable": "Y", "data_default": None},
            {"column_name": "STATUS", "data_type": "VARCHAR2", "data_length": 20,
             "nullable": "Y", "data_default": "'NEW'"},
        ],
        "pk": [{"column_name": "ORDER_ID"}],
        "fk": [{"fk_column": "CUSTOMER_ID", "ref_table": "CUSTOMERS",
                "ref_column": "CUSTOMER_ID", "delete_rule": "CASCADE"}],
        "indexes": [
            {"index_name": "PK_ORDERS", "uniqueness": "UNIQUE",
             "column_name": "ORDER_ID", "column_position": 1},
            {"index_name": "IX_ORD_CUST", "uniqueness": "NONUNIQUE",
             "column_name": "CUSTOMER_ID", "column_position": 1},
        ],
        "uniques": [{"constraint_name": "UQ_ORD_STATUS", "column_name": "STATUS"}],
        "checks": [
            {"constraint_name": "CK_STATUS", "search_condition": "status IN ('NEW','DONE')"},
            {"constraint_name": "SYS_C01", "search_condition": '"ORDER_ID" IS NOT NULL'},
        ],
        "triggers": [{"trigger_name": "TRG_ORD", "table_name": "ORDERS",
                      "triggering_event": "INSERT OR UPDATE", "trigger_type": "BEFORE EACH ROW",
                      "status": "ENABLED", "trigger_body": "BEGIN NULL; END;"}],
        "views": [{"view_name": "ACTIVE_ORDERS",
                   "text": "SELECT order_id FROM orders WHERE status='NEW'"}],
        "procs": [{"object_name": "GET_ORDER", "object_type": "PROCEDURE"}],
        "params": [
            {"argument_name": "P_ID", "data_type": "NUMBER", "in_out": "IN"},
            {"argument_name": "P_TOTAL", "data_type": "NUMBER", "in_out": "OUT"},
        ],
    }


def _adapter(ora_rows):
    return OracleAdapter("oracle://scott:tiger@dbhost:1521/orcl",
                         connect=lambda cs: _OraConn(ora_rows))


def test_trigger_events():
    assert _trigger_events("INSERT OR UPDATE") == ["INSERT", "UPDATE"]
    assert _trigger_events("DELETE") == ["DELETE"]


def test_owner_parsed_uppercase(ora_rows):
    assert _adapter(ora_rows)._owner == "SCOTT"


def test_extract_metadata(ora_rows):
    tables = _adapter(ora_rows).extract_metadata()
    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("SCOTT", "ORDERS", 1500)
    by = {c.name: c for c in t.columns}
    assert by["ORDER_ID"].is_primary_key is True
    assert by["CUSTOMER_ID"].is_foreign_key is True
    assert by["CUSTOMER_ID"].references_table == "CUSTOMERS"
    assert by["CUSTOMER_ID"].fk_on_delete == "CASCADE"
    assert by["CUSTOMER_ID"].fk_on_update is None       # Oracle has no ON UPDATE
    assert by["STATUS"].default_definition == "'NEW'"


def test_indexes_pk_detection_and_uniques(ora_rows):
    t = _adapter(ora_rows).extract_metadata()[0]
    idx = {i.name: i for i in t.indexes}
    assert idx["PK_ORDERS"].is_primary_key is True      # columns match the PK
    assert idx["IX_ORD_CUST"].is_primary_key is False
    assert idx["IX_ORD_CUST"].key_columns == ["CUSTOMER_ID"]
    assert t.unique_constraints[0].columns == ["STATUS"]


def test_checks_filter_not_null(ora_rows):
    t = _adapter(ora_rows).extract_metadata()[0]
    # the implicit NOT NULL check (SYS_C01) is filtered out
    assert [c.name for c in t.check_constraints] == ["CK_STATUS"]


def test_triggers(ora_rows):
    t = _adapter(ora_rows).extract_metadata()[0]
    assert len(t.triggers) == 1
    tr = t.triggers[0]
    assert tr.name == "TRG_ORD" and tr.events == ["INSERT", "UPDATE"]
    assert tr.is_instead_of is False and tr.is_disabled is False


def test_extract_views(ora_rows):
    views = _adapter(ora_rows).extract_views()
    assert len(views) == 1
    assert views[0].name == "ACTIVE_ORDERS"
    assert views[0].definition.startswith("SELECT")
    assert [c.name for c in views[0].columns] == ["ORDER_ID", "CUSTOMER_ID", "STATUS"]


def test_extract_procedures(ora_rows):
    procs = _adapter(ora_rows).extract_procedures()
    assert len(procs) == 1
    p = procs[0]
    assert (p.schema, p.name) == ("SCOTT", "GET_ORDER")
    assert [pm.name for pm in p.parameters] == ["P_ID", "P_TOTAL"]
    assert p.parameters[1].is_output is True


def test_build_connection_string():
    cs = OracleAdapter.build_connection_string("dbhost:1521", "orcl", "scott", "tiger")
    assert cs == "oracle://scott:tiger@dbhost:1521/orcl"


def test_missing_driver_gives_actionable_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "oracledb" or name.startswith("oracledb."):
            raise ImportError("No module named 'oracledb'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = OracleAdapter("oracle://scott:tiger@dbhost:1521/orcl")
    with pytest.raises(ImportError) as ei:
        adapter.connect()
    assert "sqldoc[oracle]" in str(ei.value)
    assert "oracledb" in str(ei.value)
