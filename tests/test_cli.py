"""CLI flag combinations and helpers, with DB + LLM mocked out."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from conftest import build_tables, build_views, build_procs


# --- pure helpers ----------------------------------------------------------

@pytest.mark.parametrize("fmt,output,expected", [
    ("html", "x.md", "html"),      # explicit --format wins
    (None, "x.md", "markdown"),
    (None, "x.markdown", "markdown"),
    (None, "x.pdf", "pdf"),
    (None, "x.html", "html"),
    (None, "x.txt", "html"),        # unknown extension -> html
])
def test_resolve_format(fmt, output, expected):
    assert cli.resolve_format(fmt, output) == expected


@pytest.mark.parametrize("cs,expected", [
    ("DRIVER={x};SERVER=h;DATABASE=Sales;UID=u;PWD=p;", "Sales"),
    ("Server=h;Initial Catalog=Warehouse;", "Warehouse"),
    ("SERVER=h;UID=u;PWD=p;", None),
])
def test_parse_database(cs, expected):
    assert cli._parse_database(cs) == expected


def test_load_config(tmp_path):
    p = tmp_path / ".sqldoc.yml"
    p.write_text("server: h\nno-ai: true\n\"yes\": true\nbogus: 1\n", encoding="utf-8")
    cfg = cli.load_config(str(p), explicit=True)
    assert cfg["server"] == "h"
    assert cfg["no_ai"] is True         # hyphen normalized
    assert cfg["yes"] is True           # YAML bare `yes:` boolean mapped back
    assert "bogus" not in cfg           # unknown key dropped


def test_load_config_missing_explicit_errors(tmp_path):
    import click
    with pytest.raises(click.UsageError):
        cli.load_config(str(tmp_path / "absent.yml"), explicit=True)


# --- full command runs (DB + AI patched) -----------------------------------

@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: build_tables())
    monkeypatch.setattr(cli, "extract_views", lambda cs: build_views())
    monkeypatch.setattr(cli, "extract_procedures", lambda cs: build_procs())
    # keep AI a no-op in case a test runs without --no-ai
    monkeypatch.setattr(cli, "enrich_tables", lambda t, **k: t)
    monkeypatch.setattr(cli, "enrich_views", lambda v, **k: v)
    monkeypatch.setattr(cli, "enrich_procedures", lambda p, **k: p)


def test_run_no_ai_html(patched, tmp_path):
    out = tmp_path / "doc.html"
    res = CliRunner().invoke(cli.main, [
        "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-ai", "--no-snapshot", "--no-cache", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert out.exists() and "Orders" in out.read_text(encoding="utf-8")


def test_format_inferred_from_extension(patched, tmp_path):
    out = tmp_path / "doc.md"
    res = CliRunner().invoke(cli.main, [
        "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-ai", "--no-snapshot", "--no-cache", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert out.read_text(encoding="utf-8").startswith("# DB")


def test_doc_json_format(patched, tmp_path):
    import json
    out = tmp_path / "doc.json"
    res = CliRunner().invoke(cli.main, [
        "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-ai", "--no-snapshot", "--no-cache", "--format", "json", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["database"] == "DB"
    assert any(t["name"] == "Orders" for t in data["tables"])


def test_missing_connection_settings_errors():
    res = CliRunner().invoke(cli.main, ["--no-ai", "--no-snapshot", "--no-cache"])
    assert res.exit_code != 0
    assert "Missing connection settings" in res.output


def test_connection_string_parses_database(patched, tmp_path):
    out = tmp_path / "doc.html"
    res = CliRunner().invoke(cli.main, [
        "--connection-string", "DRIVER={x};SERVER=h;DATABASE=Warehouse;UID=u;PWD=p;",
        "--no-ai", "--no-snapshot", "--no-cache", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert "Database: Warehouse" in res.output


def test_cloud_mode_aborts_without_confirmation(patched, tmp_path):
    res = CliRunner().invoke(cli.main, [
        "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--mode", "cloud", "--no-snapshot", "--no-cache", "--output", str(tmp_path / "d.html"),
    ], input="n\n")
    assert res.exit_code != 0
    assert "Aborted" in res.output


def test_cloud_mode_proceeds_with_yes(patched, tmp_path):
    out = tmp_path / "d.html"
    res = CliRunner().invoke(cli.main, [
        "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--mode", "cloud", "--yes", "--no-snapshot", "--no-cache", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert out.exists()


def test_invalid_concurrency_rejected(patched, tmp_path):
    res = CliRunner().invoke(cli.main, [
        "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-ai", "--no-snapshot", "--no-cache", "--concurrency", "0",
        "--output", str(tmp_path / "d.html"),
    ])
    assert res.exit_code != 0


# --- command group + scan --------------------------------------------------

def test_group_lists_subcommands():
    res = CliRunner().invoke(cli.cli, ["--help"])
    assert res.exit_code == 0
    assert "doc" in res.output and "scan" in res.output


def test_default_group_routes_bare_options_to_doc(patched, tmp_path):
    out = tmp_path / "d.html"
    res = CliRunner().invoke(cli.cli, [
        "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-ai", "--no-snapshot", "--no-cache", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert out.exists()


def test_scan_command_writes_report(monkeypatch, tmp_path):
    from sqldoc.extractor import Table, Column
    people = Table("dbo", "People", 5, columns=[
        Column("Id", "int", 4, False, True, False, None, None),
        Column("EmailAddress", "nvarchar", 100, True, False, False, None, None),
        Column("NationalID", "nvarchar", 20, True, False, False, None, None),
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [people])
    out = tmp_path / "pii.html"
    res = CliRunner().invoke(cli.cli, [
        "scan", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert "HIGH:" in res.output
    html = out.read_text(encoding="utf-8")
    assert "Email Address" in html and "National ID / SSN" in html


def test_scan_json_output(monkeypatch, tmp_path):
    import json
    from sqldoc.extractor import Table, Column
    t = Table("dbo", "People", 2, columns=[
        Column("NationalID", "nvarchar", 20, True, False, False, None, None),
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [t])
    jout = tmp_path / "findings.json"
    res = CliRunner().invoke(cli.cli, [
        "scan", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-baseline", "--json", str(jout), "--output", str(tmp_path / "p.html"),
    ])
    assert res.exit_code == 0, res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["summary"]["by_risk"]["HIGH"] == 1
    assert data["findings"][0]["category"] == "National ID / SSN"


def test_scan_fail_on_high_exits_nonzero(monkeypatch, tmp_path):
    from sqldoc.extractor import Table, Column
    t = Table("dbo", "People", 1, columns=[
        Column("NationalID", "nvarchar", 20, True, False, False, None, None),  # HIGH
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [t])
    res = CliRunner().invoke(cli.cli, [
        "scan", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-baseline", "--fail-on", "high", "--output", str(tmp_path / "p.html"),
    ])
    assert res.exit_code == 1, res.output
    assert "GATE FAILED" in res.output


def test_scan_fail_on_high_passes_without_high(monkeypatch, tmp_path):
    from sqldoc.extractor import Table, Column
    t = Table("dbo", "People", 1, columns=[
        Column("FirstName", "nvarchar", 50, True, False, False, None, None),  # LOW
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [t])
    res = CliRunner().invoke(cli.cli, [
        "scan", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-baseline", "--fail-on", "high", "--output", str(tmp_path / "p.html"),
    ])
    assert res.exit_code == 0, res.output


def test_scan_confidence_threshold_drops_weak(monkeypatch, tmp_path):
    from sqldoc.extractor import Table, Column
    # NationalID as int is a type-mismatch (confidence 0.4); threshold 0.5 drops it.
    t = Table("dbo", "People", 1, columns=[
        Column("NationalID", "int", 4, True, False, False, None, None),
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [t])
    res = CliRunner().invoke(cli.cli, [
        "scan", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--no-baseline", "--confidence-threshold", "0.5", "--output", str(tmp_path / "p.html"),
    ])
    assert res.exit_code == 0, res.output
    assert "Dropped 1 finding" in res.output
    assert "Flagged 0 column" not in res.output       # flagged before the drop
    assert "MEDIUM: 0" in res.output


def test_scan_allowlist_suppresses(monkeypatch, tmp_path):
    from sqldoc.extractor import Table, Column
    t = Table("dbo", "People", 1, columns=[
        Column("EmailAddress", "nvarchar", 100, True, False, False, None, None),
        Column("NationalID", "nvarchar", 20, True, False, False, None, None),
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [t])
    cfg = tmp_path / "c.yml"
    cfg.write_text("pii_allowlist:\n  - dbo.People.EmailAddress\n", encoding="utf-8")
    out = tmp_path / "pii.html"
    res = CliRunner().invoke(cli.cli, [
        "scan", "--config", str(cfg), "--server", "h", "--database", "DB",
        "--username", "u", "--password", "p", "--no-baseline", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert "Suppressed 1 finding" in res.output
    html = out.read_text(encoding="utf-8")
    assert "National ID / SSN" in html and "Email Address" not in html


def test_scan_custom_pii_patterns_from_config(monkeypatch, tmp_path):
    from sqldoc.extractor import Table, Column
    staff = Table("dbo", "Staff", 1, columns=[
        Column("BadgeCode", "nvarchar", 20, True, False, False, None, None),
    ])
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: [staff])
    cfg = tmp_path / "c.yml"
    cfg.write_text("pii_patterns:\n  - category: Badge\n    patterns: ['badgecode']\n    severity: LOW\n",
                   encoding="utf-8")
    out = tmp_path / "pii.html"
    res = CliRunner().invoke(cli.cli, [
        "scan", "--config", str(cfg), "--server", "h", "--database", "DB",
        "--username", "u", "--password", "p", "--no-baseline", "--output", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert "custom PII pattern" in res.output
    assert "Badge" in out.read_text(encoding="utf-8")
