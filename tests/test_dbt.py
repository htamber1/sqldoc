"""dbt integration: project discovery, parsing, merge with DB schema, render, CLI."""
import json

from click.testing import CliRunner

from sqldoc import cli
from sqldoc.dbt import (find_dbt_project, parse_dbt_project, merge, summarize)
from sqldoc.dbt_renderer import build_dbt_json, render_dbt_html
from sqldoc.extractor import Table, Column


def _table(name, cols, schema="public"):
    return Table(schema, name, 100, columns=cols)


def _col(name, dt="integer", desc=None):
    return Column(name, dt, 4, True, False, False, None, None, description=desc)


def _make_project(root, with_models=True):
    """Write a minimal dbt project under `root` (a tmp_path)."""
    (root / "dbt_project.yml").write_text(
        "name: analytics\nprofile: analytics\nversion: '1.0.0'\nmodel-paths: [\"models\"]\n",
        encoding="utf-8")
    models = root / "models"
    models.mkdir()
    if with_models:
        (models / "customers.sql").write_text("SELECT 1 AS customer_id", encoding="utf-8")
        (models / "schema.yml").write_text(
            "version: 2\n"
            "models:\n"
            "  - name: customers\n"
            "    description: One row per customer.\n"
            "    config:\n"
            "      materialized: table\n"
            "    columns:\n"
            "      - name: customer_id\n"
            "        description: Primary key.\n"
            "        tests: [not_null, unique]\n"
            "      - name: email\n"
            "        description: Contact email.\n"
            "      - name: legacy_flag\n"
            "        description: Documented but dropped from the DB.\n",
            encoding="utf-8")
    return root


# --- discovery + parsing ----------------------------------------------------

def test_find_dbt_project_direct(tmp_path):
    _make_project(tmp_path)
    assert find_dbt_project(str(tmp_path)) == str(tmp_path.resolve())


def test_find_dbt_project_in_subdir(tmp_path):
    sub = tmp_path / "warehouse"
    sub.mkdir()
    _make_project(sub)
    assert find_dbt_project(str(tmp_path)) == str(sub.resolve())


def test_find_dbt_project_none(tmp_path):
    assert find_dbt_project(str(tmp_path)) is None


def test_parse_dbt_project(tmp_path):
    _make_project(tmp_path)
    project = parse_dbt_project(str(tmp_path))
    assert project.name == "analytics"
    assert len(project.models) == 1
    m = project.models[0]
    assert m.name == "customers"
    assert m.description == "One row per customer."
    assert m.materialized == "table"
    assert [c.name for c in m.columns] == ["customer_id", "email", "legacy_flag"]
    assert m.columns[0].tests == ["not_null", "unique"]
    assert m.sql_path.endswith("customers.sql")


# --- merge with the live database schema ------------------------------------

def test_merge_matches_and_flags_gaps_and_drift(tmp_path):
    _make_project(tmp_path)
    project = parse_dbt_project(str(tmp_path))
    # DB table has customer_id + email (matched) + phone (db-only, undocumented).
    # dbt's legacy_flag has no DB column -> dbt-only (drift).
    db = _table("customers", [
        _col("customer_id"), _col("email", "varchar"), _col("phone", "varchar"),
    ])
    doc = merge(project, [db])
    m = doc.models[0]
    assert m.in_db and m.matched_table == "public.customers"
    statuses = {c.name: c.status for c in m.columns}
    assert statuses["customer_id"] == "matched"
    assert statuses["email"] == "matched"
    assert statuses["phone"] == "db-only"          # in DB, undocumented
    assert statuses["legacy_flag"] == "dbt-only"   # documented, gone from DB

    s = summarize(doc)
    assert s["models"] == 1 and s["matched_in_db"] == 1
    assert s["undocumented_db_columns"] == 1        # phone
    assert s["drifted_columns"] == 1                # legacy_flag
    assert s["doc_coverage_pct"] > 0


def test_merge_unmatched_db_table(tmp_path):
    _make_project(tmp_path)
    project = parse_dbt_project(str(tmp_path))
    doc = merge(project, [_table("orders", [_col("id")])])
    assert "public.orders" in doc.unmatched_db_tables


def test_merge_dbt_only_without_db(tmp_path):
    _make_project(tmp_path)
    project = parse_dbt_project(str(tmp_path))
    doc = merge(project, [])
    m = doc.models[0]
    assert not m.in_db
    # all dbt columns present, none matched to a DB column
    assert all(c.status == "dbt-only" for c in m.columns)


# --- render + json ----------------------------------------------------------

def test_build_and_render(tmp_path):
    _make_project(tmp_path)
    project = parse_dbt_project(str(tmp_path))
    doc = merge(project, [_table("customers", [_col("customer_id"), _col("email", "varchar")])])

    data = build_dbt_json(project.name, doc)
    assert data["report_type"] == "dbt" and data["project"] == "analytics"
    assert data["models"][0]["name"] == "customers"
    assert data["summary"]["matched_in_db"] == 1

    out = tmp_path / "dbt.html"
    render_dbt_html(project.name, doc, str(out))
    h = out.read_text(encoding="utf-8")
    assert "dbt" in h and "customers" in h and "customer_id" in h


# --- CLI --------------------------------------------------------------------

def test_dbt_cli_no_db(tmp_path):
    _make_project(tmp_path)
    out = tmp_path / "dbt.html"
    jout = tmp_path / "dbt.json"
    res = CliRunner().invoke(cli.cli, [
        "dbt", "--project-dir", str(tmp_path), "--no-db",
        "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Models: 1" in res.output
    assert out.exists()
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["report_type"] == "dbt"


def test_dbt_cli_no_project(tmp_path):
    res = CliRunner().invoke(cli.cli, ["dbt", "--project-dir", str(tmp_path), "--no-db"])
    assert res.exit_code != 0
    assert "No dbt project found" in res.output
