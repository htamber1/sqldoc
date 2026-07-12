"""Confluence connector tests — api_request is monkeypatched, no network."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.adapters.base import Capabilities
from sqldoc.integrations import confluence
from sqldoc.integrations.base import Artifact, IntegrationError
from sqldoc.integrations.reports import gather, metrics as bundle_metrics


CONFIG = {
    "base_url": "https://acme.atlassian.net/wiki/", "email": "bot@acme.com",
    "api_token": "tok", "space_key": "DBDOCS", "parent_page_id": "999",
}


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


def make_router(pages=None):
    """A fake api_request that emulates spaces/pages/attachments. `pages` maps
    title -> {id, version} for pre-existing pages."""
    pages = pages or {}
    calls = []

    def router(method, url, cfg, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/api/v2/spaces"):
            return {"results": [{"id": "SPACE1", "key": "DBDOCS"}]}
        if url.endswith("/api/v2/pages") and method == "GET":
            title = kwargs["params"]["title"]
            if title in pages:
                p = pages[title]
                return {"results": [{"id": p["id"], "title": title,
                                     "version": {"number": p["version"]}}]}
            return {"results": []}
        if url.endswith("/api/v2/pages") and method == "POST":
            return {"id": "NEWPAGE"}
        if "/api/v2/pages/" in url and method == "PUT":
            return {"id": url.rsplit("/", 1)[-1]}
        if "child/attachment" in url:
            return {"results": []}
        return {}

    router.calls = calls
    return router


def test_resolve_space_id(monkeypatch):
    monkeypatch.setattr(confluence, "api_request", make_router())
    assert confluence.resolve_space_id(CONFIG) == "SPACE1"


def test_find_page(monkeypatch):
    monkeypatch.setattr(confluence, "api_request",
                        make_router({"Database: AcmeDB": {"id": "P1", "version": 3}}))
    pid, ver = confluence.find_page(CONFIG, "SPACE1", "Database: AcmeDB")
    assert pid == "P1" and ver == 3
    pid2, ver2 = confluence.find_page(CONFIG, "SPACE1", "Nope")
    assert pid2 is None


def test_build_storage_body(bundle):
    body = confluence.build_storage_body(bundle, bundle_metrics(bundle))
    assert "<h2>Executive scorecard</h2>" in body
    assert "PII / compliance findings" in body
    assert "SSN" in body and "GDPR" in body
    assert "<h2>Documentation</h2>" in body


def test_push_creates_new_page(monkeypatch, bundle):
    router = make_router()   # no existing pages
    monkeypatch.setattr(confluence, "api_request", router)
    arts = [Artifact("AcmeDB-doc.html", "doc_html", b"<html>", "text/html")]
    res = confluence.Client(CONFIG).push_reports(arts, metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"] and "Created" in res["detail"]
    posts = [c for c in router.calls if c[0] == "POST" and c[1].endswith("/api/v2/pages")]
    assert len(posts) == 1
    assert posts[0][2]["json"]["parentId"] == "999"
    attaches = [c for c in router.calls if "child/attachment" in c[1]]
    assert len(attaches) == 1


def test_push_updates_existing_page(monkeypatch, bundle):
    router = make_router({"Database: AcmeDB": {"id": "P7", "version": 4}})
    monkeypatch.setattr(confluence, "api_request", router)
    arts = [Artifact("AcmeDB-doc.html", "doc_html", b"<html>", "text/html")]
    res = confluence.Client(CONFIG).push_reports(arts, metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"] and "Updated" in res["detail"]
    puts = [c for c in router.calls if c[0] == "PUT" and "/api/v2/pages/P7" in c[1]]
    assert len(puts) == 1
    assert puts[0][2]["json"]["version"]["number"] == 5


def test_push_requires_bundle(monkeypatch):
    monkeypatch.setattr(confluence, "api_request", make_router())
    with pytest.raises(IntegrationError):
        confluence.Client(CONFIG).push_reports([], metrics={}, bundle=None)


def test_missing_config():
    with pytest.raises(IntegrationError) as e:
        confluence.Client({"base_url": "x"}).test()
    assert "email" in str(e.value)


def test_cli_test(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(confluence, "api_request", make_router())
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"confluence": CONFIG}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["confluence", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "DBDOCS" in res.output
