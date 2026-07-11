"""Renderer output validation for HTML, Markdown, and PDF."""
import re
from xml.etree import ElementTree as ET

from sqldoc.renderer import render_html
from sqldoc.markdown_renderer import render_markdown
from sqldoc.pdf_renderer import render_pdf


def _html(tmp_path, tables, views, procs):
    out = tmp_path / "doc.html"
    render_html("TestDB", tables, str(out), views=views, procedures=procs)
    return out.read_text(encoding="utf-8")


def test_html_core_content(tmp_path, sample_tables, sample_views, sample_procs):
    h = _html(tmp_path, sample_tables, sample_views, sample_procs)
    assert "TestDB" in h
    assert "Orders" in h and "vActiveOrders" in h and "uspGetOrder" in h
    # sections + sidebar nav
    assert 'class="sidebar"' in h and 'class="nav-item"' in h
    # every nav target resolves to a card id
    hrefs = set(re.findall(r'class="nav-item" href="#(obj-[^"]+)"', h))
    ids = set(re.findall(r'class="table-card"\s+id="(obj-[^"]+)"', h))
    assert hrefs and hrefs.issubset(ids)


def test_html_computed_and_trigger(tmp_path, sample_tables, sample_views, sample_procs):
    h = _html(tmp_path, sample_tables, sample_views, sample_procs)
    assert "badge-computed" in h
    assert "([Qty]*[Price])" in h              # computed expression
    assert ">Triggers (" in h and "trOrders" in h


def test_html_row_count_color(tmp_path, sample_tables, sample_views, sample_procs):
    h = _html(tmp_path, sample_tables, sample_views, sample_procs)
    assert 'class="row-count has-data"' in h   # Orders (1596 rows)
    assert 'class="row-count no-data"' in h     # Archive (0 rows)
    assert ">1,596 rows<" in h                   # thousands separator


def test_html_escapes_sql_and_svg_wellformed(tmp_path):
    from sqldoc.extractor import Table, Column, View
    # A resolvable FK pair so the ER diagram actually renders.
    customer = Table("Sales", "Customer", 10,
                     columns=[Column("Id", "int", 4, False, True, False, None, None)])
    orders = Table("Sales", "Orders", 5, columns=[
        Column("Id", "int", 4, False, True, False, None, None),
        Column("CustomerID", "int", 4, True, False, True, "Customer", "Id"),
    ])
    # A definition containing '<=' / '>=' must be escaped, not break the markup.
    view = View("Sales", "vRange",
                columns=[Column("Id", "int", 4, False, False, False, None, None)],
                definition="CREATE VIEW vRange AS SELECT Id FROM Orders WHERE Id <= 5 AND Id >= 1;")
    out = tmp_path / "doc.html"
    render_html("DB", [customer, orders], str(out), views=[view])
    h = out.read_text(encoding="utf-8")

    assert "&lt;=" in h and "<= 5" not in h     # escaped, not raw
    svg = re.search(r'<svg id="er-svg".*?</svg>', h, re.S)
    assert svg is not None
    ET.fromstring(svg.group(0))                 # raises if malformed


def test_html_constraints(tmp_path, sample_tables, sample_views, sample_procs):
    h = _html(tmp_path, sample_tables, sample_views, sample_procs)
    assert ">Constraints (" in h
    assert "CK_Orders_Status" in h and "UQ_Orders_Customer" in h
    assert "badge-default" in h                    # Status column default badge
    assert "ON DELETE CASCADE" in h                # FK action on CustomerID


def test_markdown_constraints(tmp_path, sample_tables, sample_views, sample_procs):
    out = tmp_path / "doc.md"
    render_markdown("TestDB", sample_tables, str(out), views=sample_views, procedures=sample_procs)
    md = out.read_text(encoding="utf-8")
    assert "**Constraints**" in md
    assert "CK_Orders_Status" in md and "UQ_Orders_Customer" in md
    assert "ON DELETE CASCADE" in md
    assert "default ((0))" in md


def test_markdown_output(tmp_path, sample_tables, sample_views, sample_procs):
    out = tmp_path / "doc.md"
    render_markdown("TestDB", sample_tables, str(out), views=sample_views, procedures=sample_procs)
    md = out.read_text(encoding="utf-8")
    assert md.startswith("# TestDB")
    assert "## Tables" in md and "### Sales.Orders" in md
    assert "## Views" in md and "## Stored Procedures" in md
    assert "```sql" in md                       # fenced definition
    assert "computed" in md
    assert "**Triggers**" in md


def test_pdf_output(tmp_path, sample_tables, sample_views, sample_procs):
    out = tmp_path / "doc.pdf"
    render_pdf("TestDB", sample_tables, str(out), views=sample_views, procedures=sample_procs)
    data = out.read_bytes()
    assert data[:5] == b"%PDF-"
    assert len(data) > 1000
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() for p in PdfReader(str(out)).pages)
    for needle in ["TestDB", "Orders", "vActiveOrders", "uspGetOrder", "Trigger"]:
        assert needle in text
