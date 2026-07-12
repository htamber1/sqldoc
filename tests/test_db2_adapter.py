"""IBM Db2 adapter with a fake cursor emulating ibm_db_dbi (description + tuples)."""
import builtins
import pytest

from sqldoc.adapters import detect_dialect, get_adapter, DIALECTS
from sqldoc.adapters.db2 import Db2Adapter, _split_colnames


def _route(sql):
    if "SYSCAT.TABLESPACES" in sql:
        return "tablespaces"
    if "MON_GET_BUFFERPOOL" in sql:
        return "bufferpools"
    if "MON_LOCKWAITS" in sql:
        return "lockwaits"
    if "SYSCAT.TABLES" in sql:
        return "tables"
    if "SYSCAT.COLUMNS" in sql:
        return "columns"
    if "SYSCAT.INDEXES" in sql:
        return "indexes"
    if "SYSCAT.VIEWS" in sql:
        return "views"
    if "SYSCAT.ROUTINES" in sql:
        return "routines"
    return "unknown"


class _Db2Cursor:
    def __init__(self, data):
        self._data = data
        self._rows = []
        self._desc = []

    def execute(self, sql, params=None):
        rows = self._data.get(_route(sql), [])
        self._rows = rows
        keys = list(rows[0].keys()) if rows else []
        self._desc = [(k.upper(),) for k in keys]
        return self

    @property
    def description(self):
        return self._desc

    def fetchall(self):
        # emulate tuple rows (positional), like ibm_db_dbi
        keys = [d[0].lower() for d in self._desc]
        return [tuple(r[k] for k in keys) for r in self._rows]


class _Db2Conn:
    def __init__(self, data):
        self._data = data

    def cursor(self):
        return _Db2Cursor(self._data)

    def close(self):
        pass


@pytest.fixture
def db2_rows():
    return {
        "tables": [{"tabschema": "APP", "tabname": "ORDERS", "card": 12000,
                    "tbspace": "USERSPACE1", "remarks": "Order facts"}],
        "columns": [
            {"colname": "ORDER_ID", "typename": "INTEGER", "length": 4, "nulls": "N",
             "keyseq": 1, "remarks": "PK"},
            {"colname": "TOTAL", "typename": "DECIMAL", "length": 9, "nulls": "Y",
             "keyseq": None, "remarks": None},
        ],
        "indexes": [{"tabschema": "APP", "tabname": "ORDERS", "indname": "PK_ORDERS",
                     "uniquerule": "P", "colnames": "+ORDER_ID"}],
        "views": [{"viewschema": "APP", "viewname": "V_ORDERS", "text": "SELECT * FROM APP.ORDERS"}],
        "routines": [{"routineschema": "APP", "routinename": "SP_LOAD", "text": "BEGIN END"}],
        "tablespaces": [{"tbspace": "USERSPACE1", "tbspacetype": "D", "pagesize": 4096, "bufferpoolid": 1}],
        "bufferpools": [{"bp_name": "IBMDEFAULTBP", "pool_data_l_reads": 1000, "pool_data_p_reads": 50}],
        "lockwaits": [{"hld_application_handle": 12, "req_application_handle": 34,
                       "lock_mode": "X", "lock_object_type": "TABLE", "lock_wait_elapsed_time": 4500}],
    }


def _adapter(db2_rows):
    return Db2Adapter("db2://u:p@host:50000/SAMPLE", connect=lambda cs: _Db2Conn(db2_rows))


def test_detection_and_registration():
    assert detect_dialect("db2://u:p@host:50000/SAMPLE") == "db2"
    assert detect_dialect("ibm-db2://u:p@host/db") == "db2"
    assert DIALECTS["db2"] is Db2Adapter
    a = get_adapter("db2://h/db", "db2")
    assert a.dialect == "db2" and not a.capabilities.health


def test_split_colnames():
    assert _split_colnames("+ORDER_ID+CUSTOMER_ID-STATUS") == ["ORDER_ID", "CUSTOMER_ID", "STATUS"]
    assert _split_colnames("") == []


def test_extract_metadata(db2_rows):
    tables = _adapter(db2_rows).extract_metadata()
    assert len(tables) == 1
    t = tables[0]
    assert (t.schema, t.name, t.row_count) == ("APP", "ORDERS", 12000)
    assert "[Db2 tablespace: USERSPACE1]" in t.description and "Order facts" in t.description
    by = {c.name: c for c in t.columns}
    assert by["ORDER_ID"].is_primary_key and not by["TOTAL"].is_primary_key
    assert t.indexes[0].name == "PK_ORDERS" and t.indexes[0].is_primary_key


def test_extract_views_and_procs(db2_rows):
    a = _adapter(db2_rows)
    assert a.extract_views()[0].name == "V_ORDERS"
    assert a.extract_procedures()[0].name == "SP_LOAD"


def test_db2_operational(db2_rows):
    a = _adapter(db2_rows)
    ts = a.db2_tablespaces()
    assert ts[0]["tablespace"] == "USERSPACE1" and ts[0]["page_size"] == 4096
    bp = a.db2_bufferpools()
    assert bp[0]["hit_ratio_pct"] == 95.0            # (1000-50)/1000
    lw = a.db2_lock_waits()
    assert lw[0]["lock_mode"] == "X" and lw[0]["wait_ms"] == 4500


def test_missing_driver_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "ibm_db_dbi":
            raise ImportError("no ibm_db")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError) as ei:
        Db2Adapter("db2://h/db").connect()
    assert "sqldoc[db2]" in str(ei.value)
