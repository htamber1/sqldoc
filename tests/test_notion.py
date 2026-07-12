"""Notion connector tests — notion_request monkeypatched, no network."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.adapters.base import Capabilities
from sqldoc.integrations import notion
from sqldoc.integrations.base import IntegrationError
from sqldoc.integrations.reports import gather, metrics as bundle_metrics


CONFIG = {"token": "secret_x", "parent_page_id": "PARENT", "database_id": "TRACKER"}


class _Adapter:
    def __init__(self, tables):
        self._t = tables
        self.dialect = "sqlserver"
        self.display_name = "SQL Server"
        self.capabilities = Capabilities()

    def extract_metadata(self):
        return list(self._t)

    def extract_views(self):
        return []

    def extract_procedures(self):
        return []


@pytest.fixture
def bundle(sample_tables):
    from sqldoc.pii import Finding
    b = gather(_Adapter(sample_tables), "AcmeDB")
    b.findings.append(Finding("Sales", "Customer", "SSN", "varchar", "government_id",
                              "HIGH", "name+type", ["GDPR"], "Encrypt"))
    return b


def make_router(existing_tracker_row=False):
    calls = []

    def router(method, path, cfg, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/users/me":
            return {"name": "sqldoc-bot"}
        if path == "/search":
            return {"results": []}
        if path == "/pages" and method == "POST":
            return {"id": "PAGE1", "url": "https://notion.so/PAGE1"}
        if path == "/databases" and method == "POST":
            return {"id": "FINDDB"}
        if path.endswith("/query"):
            return {"results": ([{"id": "ROW1"}] if existing_tracker_row else [])}
        if path.startswith("/pages/") and method == "PATCH":
            return {"id": path.rsplit("/", 1)[-1]}
        return {}

    router.calls = calls
    return router


def test_test_ok(monkeypatch):
    monkeypatch.setattr(notion, "notion_request", make_router())
    res = notion.Client(CONFIG).test()
    assert res["ok"] and "sqldoc-bot" in res["detail"]


def test_build_blocks(bundle):
    blocks = notion.build_blocks(bundle, bundle_metrics(bundle))
    kinds = [b["type"] for b in blocks]
    assert "heading_2" in kinds and "bulleted_list_item" in kinds


def test_metric_properties_numbers(bundle):
    props = notion.metric_properties(bundle_metrics(bundle))
    assert props["Name"]["title"]
    assert "number" in props["PII Findings"]
    assert props["Scan Date"]["date"]["start"]


def test_push_creates_page_findings_and_tracker(monkeypatch, bundle):
    router = make_router(existing_tracker_row=False)
    monkeypatch.setattr(notion, "notion_request", router)
    res = notion.Client(CONFIG).push_reports([], metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"] and res["url"] == "https://notion.so/PAGE1"
    # doc page created, findings database created, at least one finding row, tracker row created
    assert any(c[1] == "/databases" for c in router.calls)
    page_posts = [c for c in router.calls if c[1] == "/pages" and c[0] == "POST"]
    assert len(page_posts) >= 3   # doc page + finding row + tracker row
    assert "created" in res["detail"] or "tracker row created" in res["detail"]


def test_push_updates_existing_tracker(monkeypatch, bundle):
    router = make_router(existing_tracker_row=True)
    monkeypatch.setattr(notion, "notion_request", router)
    res = notion.Client(CONFIG).push_reports([], metrics=bundle_metrics(bundle), bundle=bundle)
    assert "tracker row updated" in res["detail"]
    patches = [c for c in router.calls if c[0] == "PATCH" and c[1] == "/pages/ROW1"]
    assert len(patches) == 1


def test_push_without_tracker(monkeypatch, bundle):
    router = make_router()
    monkeypatch.setattr(notion, "notion_request", router)
    cfg = {"token": "t", "parent_page_id": "PARENT"}   # no database_id
    res = notion.Client(cfg).push_reports([], metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"] and "tracker" not in res["detail"]


def test_missing_config():
    with pytest.raises(IntegrationError):
        notion.Client({}).test()


def test_cli_test(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(notion, "notion_request", make_router())
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"notion": CONFIG}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["notion", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "sqldoc-bot" in res.output
