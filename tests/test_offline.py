"""Air-gap / offline verification: the detector, the shipped templates, the CLI."""
import pytest

from sqldoc.offline import find_external_refs, blocking_refs, verify_file
from sqldoc.renderer import HTML_TEMPLATE
from sqldoc.pii_renderer import PII_TEMPLATE
from sqldoc.health_renderer import HEALTH_TEMPLATE
from sqldoc.quality_renderer import QUALITY_TEMPLATE
from sqldoc.intel_renderer import INTEL_TEMPLATE
from sqldoc.insights_renderer import INSIGHTS_TEMPLATE
from sqldoc.comply_renderer import COMPLY_TEMPLATE
from sqldoc.dbt_renderer import DBT_TEMPLATE
from sqldoc.comply_multi_renderer import MULTI_TEMPLATE
from sqldoc.server_renderer import SERVER_TEMPLATE
from sqldoc.logs_renderer import LOGS_TEMPLATE


# --- detector ---------------------------------------------------------------

def test_detects_cdn_script():
    html = '<html><head><script src="https://cdn.example.com/app.js"></script></head></html>'
    refs = find_external_refs(html)
    assert any(r.kind == "src" and "cdn.example.com" in r.url for r in refs)
    assert blocking_refs(refs)


def test_detects_external_stylesheet_and_font():
    html = (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Roboto">'
        '<style>@import url("https://cdn.x.com/base.css");'
        'body { background: url(//img.cdn.net/bg.png); }</style>'
    )
    refs = find_external_refs(html)
    kinds = {r.kind for r in refs}
    assert "link-href" in kinds        # external stylesheet
    assert "css-import" in kinds       # @import
    assert "css-url" in kinds          # protocol-relative url()
    assert len(blocking_refs(refs)) == 3


def test_ignores_xmlns_data_uris_and_anchors():
    html = (
        '<svg xmlns="http://www.w3.org/2000/svg"><use xlink:href="#icon"/></svg>'
        '<img src="data:image/png;base64,AAAA">'
        '<a href="#top">top</a><a href="mailto:x@y.com">mail</a>'
        '<link rel="stylesheet" href="styles.css">'          # relative -> local
    )
    refs = find_external_refs(html)
    assert refs == []                  # nothing external


def test_plain_hyperlink_is_reported_but_not_blocking():
    html = '<a href="https://github.com/htamber1/sqldoc">repo</a>'
    refs = find_external_refs(html)
    assert len(refs) == 1
    assert refs[0].kind == "a-link"
    assert not refs[0].is_blocking     # navigational, not auto-loaded
    assert blocking_refs(refs) == []


# --- the shipped report templates must all be self-contained ----------------

_TEMPLATES = {
    "doc": HTML_TEMPLATE,
    "scan": PII_TEMPLATE,
    "health": HEALTH_TEMPLATE,
    "quality": QUALITY_TEMPLATE,
    "intel": INTEL_TEMPLATE,
    "insights": INSIGHTS_TEMPLATE,
    "comply": COMPLY_TEMPLATE,
    "dbt": DBT_TEMPLATE,
    "comply-multi": MULTI_TEMPLATE,
    "server": SERVER_TEMPLATE,
    "logs": LOGS_TEMPLATE,
}


@pytest.mark.parametrize("name", list(_TEMPLATES))
def test_report_templates_are_air_gap_safe(name):
    blocking = blocking_refs(find_external_refs(_TEMPLATES[name]))
    assert blocking == [], f"{name} template has external resource refs: {blocking}"


# --- CLI integration --------------------------------------------------------

def test_cli_verify_offline_reports_ok(monkeypatch, fake_health_rows, tmp_path):
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.adapters.sqlserver import SqlServerAdapter
    from conftest import FakeConnection

    monkeypatch.setattr(SqlServerAdapter, "_default_connect",
                        staticmethod(lambda cs: FakeConnection(fake_health_rows)))
    out = tmp_path / "health.html"
    res = CliRunner().invoke(cli.cli, [
        "health", "--server", "h", "--database", "DB", "--username", "u", "--password", "p",
        "--output", str(out), "--verify-offline",
    ])
    assert res.exit_code == 0, res.output
    assert "offline check: OK" in res.output


def test_verify_file_roundtrip(tmp_path):
    clean = tmp_path / "clean.html"
    clean.write_text("<html><body><h1>hi</h1></body></html>", encoding="utf-8")
    assert verify_file(str(clean)) == []

    dirty = tmp_path / "dirty.html"
    dirty.write_text('<script src="https://cdn.x/app.js"></script>', encoding="utf-8")
    assert blocking_refs(verify_file(str(dirty)))
