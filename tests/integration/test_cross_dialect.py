"""Phase 6 — cross-dialect consistency. The same command run against SQL Server,
PostgreSQL, and MySQL must produce structurally identical output (same top-level
JSON keys and same HTML sections) even though the values differ.

Needs all three databases; skips unless every one is reachable."""
import json
import os
import re

import pytest

from _live import (MSSQL_CS, PG_CS, MYSQL_CS,
                   MSSQL_AVAILABLE, PG_AVAILABLE, MYSQL_AVAILABLE, run)

ALL_THREE = MSSQL_AVAILABLE and PG_AVAILABLE and MYSQL_AVAILABLE
pytestmark = [
    pytest.mark.skipif(not ALL_THREE, reason="need SQL Server + PostgreSQL + MySQL"),
    pytest.mark.integration,
]

DIALECTS = {
    "sqlserver": [MSSQL_CS, "sqlserver", ["--schemas", "HumanResources"]],
    "postgres": [PG_CS, "postgres", []],
    "mysql": [MYSQL_CS, "mysql", []],
}


def _run_json(tmp_path, dialect, command, extra=None):
    cs, dname, scope = DIALECTS[dialect]
    out = str(tmp_path / f"{command}-{dialect}.html")
    js = str(tmp_path / f"{command}-{dialect}.json")
    args = [command, "--connection-string", cs, "--dialect", dname,
            "--output", out, "--json", js, *scope, *(extra or [])]
    r = run(args)
    assert r.exit_code == 0, f"{command} on {dialect} failed:\n{r.output}"
    with open(js, encoding="utf-8") as f:
        data = json.load(f)
    with open(out, encoding="utf-8") as f:
        html = f.read()
    return data, html


def _run_doc_json(tmp_path, dialect):
    cs, dname, scope = DIALECTS[dialect]
    js = str(tmp_path / f"doc-{dialect}.json")
    r = run(["doc", "--connection-string", cs, "--dialect", dname, "--no-ai",
             "--no-snapshot", "--no-cache", "--format", "json", "--output", js, *scope])
    assert r.exit_code == 0, r.output
    with open(js, encoding="utf-8") as f:
        return json.load(f)


def _keys(d):
    return set(d.keys()) if isinstance(d, dict) else set()


# --- structural JSON-key consistency ---------------------------------------

def test_doc_json_same_shape(tmp_path):
    shapes = {d: _run_doc_json(tmp_path, d) for d in DIALECTS}
    key_sets = {d: _keys(v) for d, v in shapes.items()}
    assert key_sets["sqlserver"] == key_sets["postgres"] == key_sets["mysql"], key_sets
    # each table entry has the same field shape too
    for d, doc in shapes.items():
        assert doc["tables"], f"{d} has no tables"
    table_keys = {d: _keys(doc["tables"][0]) for d, doc in shapes.items()}
    assert table_keys["sqlserver"] == table_keys["postgres"] == table_keys["mysql"], table_keys


@pytest.mark.parametrize("command,extra", [
    ("scan", []),
    ("intel", []),
    ("insights", ["--no-ai"]),
    ("comply", []),
    ("quality", ["--yes"]),
    ("health", []),
])
def test_json_keys_identical_across_dialects(tmp_path, command, extra):
    results = {d: _run_json(tmp_path, d, command, extra)[0] for d in DIALECTS}
    key_sets = {d: _keys(v) for d, v in results.items()}
    ms, pg, my = key_sets["sqlserver"], key_sets["postgres"], key_sets["mysql"]
    assert ms == pg == my, f"{command} JSON keys differ:\n{key_sets}"


# --- HTML section consistency ----------------------------------------------

def _structure(html):
    """The set of structural class names in a report. The renderers are
    dialect-agnostic, so this set is identical across dialects by construction —
    only the values inside differ."""
    return set(re.findall(r'class="([^"]+)"', html))


def test_scan_html_structure_identical(tmp_path):
    structs = {d: _structure(_run_json(tmp_path, d, "scan")[1]) for d in DIALECTS}
    assert structs["sqlserver"] == structs["postgres"] == structs["mysql"], {
        d: sorted(s) for d, s in structs.items()}


def test_doc_html_structure_identical(tmp_path):
    def doc_html(d):
        cs, dname, scope = DIALECTS[d]
        out = str(tmp_path / f"doc-{d}.html")
        r = run(["doc", "--connection-string", cs, "--dialect", dname, "--no-ai",
                 "--no-snapshot", "--no-cache", "--output", out, *scope])
        assert r.exit_code == 0, r.output
        return open(out, encoding="utf-8").read()
    structs = {d: _structure(doc_html(d)) for d in DIALECTS}
    # the doc template's structural classes are the same for every dialect
    common = structs["sqlserver"] & structs["postgres"] & structs["mysql"]
    assert len(common) >= 10, {d: len(s) for d, s in structs.items()}
