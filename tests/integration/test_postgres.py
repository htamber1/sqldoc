"""Phase 4 — live PostgreSQL integration tests against Pagila.

Provision with: bash tests/integration/docker/setup_postgres.sh
Runs the dialect-neutral + PG-supported command suite end-to-end and validates
the HTML/JSON output. Skips cleanly when Pagila is unreachable."""
import json
import os

import pytest

from _live import PG_CS, requires_pg, run

pytestmark = [requires_pg, pytest.mark.integration]

BASE = ["--connection-string", PG_CS, "--dialect", "postgres"]


def _out(tmp_path, name):
    return str(tmp_path / name)


def _assert_html(path):
    assert os.path.exists(path)
    text = open(path, encoding="utf-8").read()
    assert len(text) > 500 and "<" in text


def _assert_json(path):
    assert os.path.exists(path)
    data = json.loads(open(path, encoding="utf-8").read())
    assert data
    return data


def test_doc_html_and_json(tmp_path):
    html, js = _out(tmp_path, "doc.html"), _out(tmp_path, "doc.json")
    r = run(["doc", *BASE, "--no-ai", "--no-snapshot", "--no-cache", "--output", html])
    assert r.exit_code == 0, r.output
    _assert_html(html)
    # a known Pagila table appears in the docs
    assert "film" in open(html, encoding="utf-8").read().lower()
    r2 = run(["doc", *BASE, "--no-ai", "--no-snapshot", "--no-cache", "--format", "json", "--output", js])
    assert r2.exit_code == 0, r2.output
    data = _assert_json(js)
    assert len(data["tables"]) >= 15       # Pagila has ~22 base tables


CASES = [
    ("scan", ["scan"]),
    ("health", ["health"]),
    ("quality", ["quality", "--yes"]),
    ("intel", ["intel"]),
    ("insights", ["insights", "--no-ai"]),
    ("comply", ["comply"]),
]


@pytest.mark.parametrize("name,argv", CASES, ids=[c[0] for c in CASES])
def test_command(tmp_path, name, argv):
    html, js = _out(tmp_path, f"{name}.html"), _out(tmp_path, f"{name}.json")
    r = run([*argv, *BASE, "--output", html, "--json", js])
    assert r.exit_code == 0, f"{name} failed:\n{r.output}"
    _assert_html(html)
    _assert_json(js)


def test_scan_finds_pii(tmp_path):
    """Pagila's customer/staff tables have email + address PII."""
    js = _out(tmp_path, "scan.json")
    r = run(["scan", *BASE, "--output", _out(tmp_path, "s.html"), "--json", js])
    assert r.exit_code == 0, r.output
    data = _assert_json(js)
    assert data["summary"]["total"] > 0
