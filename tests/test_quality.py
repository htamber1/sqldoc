"""Data-quality profiling: aggregate parsing, rendering, and CLI (pyodbc mocked)."""
import json

from click.testing import CliRunner

from sqldoc import quality, cli
from sqldoc.quality import analyze_column_quality, detect_duplicates
from sqldoc.quality_renderer import build_quality_json, render_quality_html
from sqldoc.extractor import Column
from conftest import FakeConnection, build_tables


def test_analyze_column_quality(fake_quality_rows):
    cur = FakeConnection(fake_quality_rows).cursor()
    cq = analyze_column_quality(cur, "Sales", "Orders", "Status", "int", top_values=5)
    assert cq.total_rows == 100 and cq.null_count == 60
    assert cq.null_rate == 0.6
    assert cq.distinct_count == 1 and cq.is_constant
    assert set(cq.flags) == {"high-null", "constant", "blanks"}
    assert cq.top_values == [{"value": "0", "count": 40}]
    assert cq.min_value == "0" and cq.max_value == "9"


def test_detect_duplicates(fake_quality_rows):
    cur = FakeConnection(fake_quality_rows).cursor()
    cols = [Column("Id", "int", 4, False, True, False, None, None)]
    dg = detect_duplicates(cur, "Sales", "Orders", cols)
    assert dg.duplicate_groups == 3
    assert dg.duplicate_rows == 5           # sum(cnt)=8 minus 3 groups
    assert dg.columns_considered == ["Id"]


def test_detect_duplicates_none_when_no_groupable_columns(fake_quality_rows):
    cur = FakeConnection(fake_quality_rows).cursor()
    # a single computed column -> nothing to group by
    computed = Column("Calc", "int", 4, True, False, False, None, None, is_computed=True)
    assert detect_duplicates(cur, "Sales", "Orders", [computed]) is None


def test_collect_quality_pipeline(monkeypatch, fake_quality_rows):
    monkeypatch.setattr(quality, "get_connection", lambda cs: FakeConnection(fake_quality_rows))
    report = quality.collect_quality("cs", build_tables(), top_values=5)
    # Orders: Id, CustomerID, Status (LineTotal is computed -> skipped); Archive: Id
    assert len(report.columns) == 4
    assert len(report.duplicates) == 2       # both tables report duplicates in the fixture
    s = quality.summarize(report)
    assert s["constant_columns"] == 4 and s["high_null_columns"] == 4
    assert s["duplicate_rows"] == 10         # 5 per table


def test_collect_quality_no_duplicates_flag(monkeypatch, fake_quality_rows):
    monkeypatch.setattr(quality, "get_connection", lambda cs: FakeConnection(fake_quality_rows))
    report = quality.collect_quality("cs", build_tables(), detect_dupes=False)
    assert report.duplicates == []


def test_build_quality_json(monkeypatch, fake_quality_rows):
    monkeypatch.setattr(quality, "get_connection", lambda cs: FakeConnection(fake_quality_rows))
    report = quality.collect_quality("cs", build_tables())
    data = build_quality_json("DB", report)
    assert data["report_type"] == "quality"
    assert data["summary"]["columns_profiled"] == 4
    assert data["columns"][0]["flags"]                 # flags surfaced in JSON
    assert data["duplicates"][0]["duplicate_rows"] == 5


def test_render_quality_html(monkeypatch, fake_quality_rows, tmp_path):
    monkeypatch.setattr(quality, "get_connection", lambda cs: FakeConnection(fake_quality_rows))
    report = quality.collect_quality("cs", build_tables())
    out = tmp_path / "q.html"
    render_quality_html("DB", report, str(out))
    h = out.read_text(encoding="utf-8")
    assert "Data Quality" in h and "DB" in h
    assert "high-null" in h and "constant" in h
    assert "Duplicate records" in h


def test_quality_cli(monkeypatch, fake_quality_rows, tmp_path):
    monkeypatch.setattr(cli, "extract_metadata", lambda cs: build_tables())
    monkeypatch.setattr(quality, "get_connection", lambda cs: FakeConnection(fake_quality_rows))
    out = tmp_path / "q.html"
    jout = tmp_path / "q.json"
    res = CliRunner().invoke(cli.cli, [
        "quality", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--yes", "--output", str(out), "--json", str(jout),
    ])
    assert res.exit_code == 0, res.output
    assert "Columns: 4" in res.output
    data = json.loads(jout.read_text(encoding="utf-8"))
    assert data["report_type"] == "quality"
    assert data["summary"]["tables_with_duplicates"] == 2
