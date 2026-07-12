"""Phase 3 — live SQL Server integration tests against the Docker container
(AdventureWorks2022). Every SQL-Server-facing command is run end-to-end with
--no-ai for speed; the run must succeed and the HTML/JSON output must contain the
expected content. Skips cleanly when the database is unreachable."""
import json
import os

import pytest

from _live import MSSQL_CS, requires_mssql, run

pytestmark = [requires_mssql, pytest.mark.integration]

# Common args pointing at the live database. HumanResources keeps doc/scan/quality
# fast (a handful of tables) while still exercising the full path.
BASE = ["--connection-string", MSSQL_CS, "--dialect", "sqlserver"]


def _out(tmp_path, name):
    return str(tmp_path / name)


def _assert_html(path, *must_contain):
    assert os.path.exists(path), f"missing HTML output {path}"
    text = open(path, encoding="utf-8").read()
    assert len(text) > 500 and "<" in text
    for token in must_contain:
        assert token in text, f"expected {token!r} in {path}"


def _assert_json(path, *keys):
    assert os.path.exists(path), f"missing JSON output {path}"
    data = json.loads(open(path, encoding="utf-8").read())
    assert isinstance(data, (dict, list)) and data
    for k in keys:
        assert (k in data) if isinstance(data, dict) else True
    return data


# --- doc -------------------------------------------------------------------

def test_doc_html(tmp_path):
    out = _out(tmp_path, "doc.html")
    r = run(["doc", *BASE, "--no-ai", "--no-snapshot", "--no-cache",
             "--schemas", "HumanResources", "--output", out])
    assert r.exit_code == 0, r.output
    _assert_html(out, "Employee")


def test_doc_json(tmp_path):
    out = _out(tmp_path, "doc.json")
    r = run(["doc", *BASE, "--no-ai", "--no-snapshot", "--no-cache",
             "--schemas", "HumanResources", "--format", "json", "--output", out])
    assert r.exit_code == 0, r.output
    data = _assert_json(out, "tables")
    assert len(data["tables"]) > 0


# --- scan ------------------------------------------------------------------

def test_scan(tmp_path):
    html, js = _out(tmp_path, "scan.html"), _out(tmp_path, "scan.json")
    r = run(["scan", *BASE, "--schemas", "HumanResources", "--output", html, "--json", js])
    assert r.exit_code == 0, r.output
    _assert_html(html)
    data = _assert_json(js, "findings", "summary")
    assert isinstance(data["findings"], list)


# --- the analysis commands (parametrized) ----------------------------------

CASES = [
    ("health", ["health"], None),
    ("quality", ["quality", "--yes", "--schemas", "HumanResources"], None),
    ("intel", ["intel"], None),
    ("insights", ["insights", "--no-ai"], None),
    ("comply", ["comply", "--schemas", "HumanResources"], None),
    ("server", ["server"], None),
    ("logs", ["logs", "--last-hours", "72"], None),
    ("secure", ["secure"], None),
    ("waits", ["waits", "--no-ai"], None),
    ("ha", ["ha"], None),
    ("deadlocks", ["deadlocks", "--no-ai"], None),
    ("plans", ["plans", "--no-ai"], None),
    ("executive", ["executive", "--no-baseline"], None),
]


@pytest.mark.parametrize("name,argv,extra", CASES, ids=[c[0] for c in CASES])
def test_command_runs_and_emits_output(tmp_path, name, argv, extra):
    html, js = _out(tmp_path, f"{name}.html"), _out(tmp_path, f"{name}.json")
    r = run([*argv, *BASE, "--output", html, "--json", js])
    assert r.exit_code == 0, f"{name} failed:\n{r.output}"
    _assert_html(html)
    _assert_json(js)


# --- baseline capture ------------------------------------------------------

def test_baseline_capture(tmp_path):
    snap = _out(tmp_path, "baseline.json")
    html = _out(tmp_path, "baseline.html")
    r = run(["baseline", *BASE, "--capture", "--baseline-file", snap, "--output", html])
    assert r.exit_code == 0, r.output
    assert os.path.exists(snap)
    # A second run compares against the captured baseline.
    r2 = run(["baseline", *BASE, "--baseline-file", snap, "--output", html])
    assert r2.exit_code == 0, r2.output


# --- secure --fail-under gate (real score) ---------------------------------

def test_secure_score_present(tmp_path):
    js = _out(tmp_path, "secure.json")
    r = run(["secure", *BASE, "--output", _out(tmp_path, "s.html"), "--json", js])
    assert r.exit_code == 0, r.output
    data = json.loads(open(js, encoding="utf-8").read())
    assert "score" in data or "grade" in data or "summary" in data
