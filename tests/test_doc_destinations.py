"""Additional documentation destinations: GitHub/GitLab/Azure DevOps wikis,
OneDrive, Dropbox, Nuclino. All transports mocked."""
import pytest

from sqldoc.adapters.base import Capabilities
from sqldoc.integrations import (github_wiki, gitlab_wiki, azuredevops_wiki,
                                 onedrive, dropbox, nuclino, sharepoint, get_client)
from sqldoc.integrations.base import Artifact, IntegrationError
from sqldoc.integrations.reports import gather, bundle_markdown, metrics as bundle_metrics


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
                              "HIGH", "name", ["GDPR"], "Encrypt"))
    return b


ARTS = [Artifact("db-doc.html", "doc_html", b"<html>", "text/html"),
        Artifact("db-exec.html", "executive_html", b"<html>", "text/html")]


# --- markdown builder ------------------------------------------------------

def test_bundle_markdown(bundle):
    md = bundle_markdown(bundle, bundle_metrics(bundle))
    assert "# Database: AcmeDB" in md
    assert "## PII / compliance findings" in md and "SSN" in md
    assert "| Overall |" in md


# --- registry --------------------------------------------------------------

@pytest.mark.parametrize("name", ["github_wiki", "gitlab_wiki", "azuredevops_wiki",
                                  "onedrive", "dropbox", "nuclino"])
def test_registered(name):
    client = get_client(name, {})
    assert hasattr(client, "test") and hasattr(client, "push_reports")


# --- GitHub wiki -----------------------------------------------------------

def test_github_wiki_push(monkeypatch, bundle):
    calls = []
    monkeypatch.setattr(github_wiki, "run_git", lambda args, cwd=None: calls.append(" ".join(args)) or "")
    res = github_wiki.Client({"repo": "acme/db", "token": "t"}).push_reports(
        ARTS, metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"] and res["page"] == "AcmeDB"
    joined = "\n".join(calls)
    assert "clone" in joined and "commit" in joined and "push origin HEAD" in joined


def test_github_wiki_needs_bundle(monkeypatch):
    monkeypatch.setattr(github_wiki, "run_git", lambda *a, **k: "")
    with pytest.raises(IntegrationError):
        github_wiki.Client({"repo": "a/b", "token": "t"}).push_reports([], bundle=None)


# --- GitLab wiki -----------------------------------------------------------

def make_gitlab_router(exists=False):
    calls = []

    def router(method, path, cfg, **k):
        calls.append((method, path, k))
        if path.startswith("/projects/") and path.count("/") == 2 and method == "GET":
            return {"name": "DB Docs"}
        if "/wikis/" in path and method == "GET":
            return {"slug": "AcmeDB"} if exists else None
        return {}
    router.calls = calls
    return router


def test_gitlab_wiki_creates(monkeypatch, bundle):
    router = make_gitlab_router(exists=False)
    monkeypatch.setattr(gitlab_wiki, "gitlab_request", router)
    res = gitlab_wiki.Client({"project_id": "123", "token": "t"}).push_reports(
        ARTS, metrics=bundle_metrics(bundle), bundle=bundle)
    assert "Created" in res["detail"]
    assert any(m == "POST" and p == "/projects/123/wikis" for (m, p, _k) in router.calls)


def test_gitlab_wiki_updates(monkeypatch, bundle):
    router = make_gitlab_router(exists=True)
    monkeypatch.setattr(gitlab_wiki, "gitlab_request", router)
    res = gitlab_wiki.Client({"project_id": "123", "token": "t"}).push_reports(
        ARTS, metrics=bundle_metrics(bundle), bundle=bundle)
    assert "Updated" in res["detail"]
    assert any(m == "PUT" for (m, p, _k) in router.calls)


# --- Azure DevOps wiki -----------------------------------------------------

def test_azuredevops_wiki_push(monkeypatch, bundle):
    calls = []

    def fake(method, url, cfg, **k):
        calls.append((method, url))
        if method == "GET" and "/pages" in url:
            return 404, None, None      # page doesn't exist -> create
        return 200, {}, "etag"
    monkeypatch.setattr(azuredevops_wiki, "wiki_request", fake)
    res = azuredevops_wiki.Client({"organization": "acme", "project": "Data", "pat": "t"}).push_reports(
        ARTS, metrics=bundle_metrics(bundle), bundle=bundle)
    assert "Created" in res["detail"]
    assert any(m == "PUT" for (m, _u) in calls)


# --- OneDrive --------------------------------------------------------------

def test_onedrive_push(monkeypatch, bundle):
    puts = []
    monkeypatch.setattr(onedrive, "acquire_token", lambda cfg: "TOKEN")

    def fake_graph(method, url, token, **k):
        if method == "PUT":
            puts.append(url)
            return {"webUrl": "https://od/" + url.rsplit("/", 2)[-2]}
        return {"name": "OneDrive"}
    monkeypatch.setattr(onedrive, "graph_request", fake_graph)
    cfg = {"tenant_id": "t", "client_id": "c", "client_secret": "s", "user_id": "u@acme.com"}
    res = onedrive.Client(cfg).push_reports(ARTS, metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"] and len(puts) == 2
    assert "/users/u@acme.com/drive" in puts[0]


def test_onedrive_needs_target():
    with pytest.raises(IntegrationError):
        onedrive.Client({"tenant_id": "t", "client_id": "c", "client_secret": "s"})._drive_base()


# --- Dropbox ---------------------------------------------------------------

def test_dropbox_push(monkeypatch, bundle):
    uploads = []
    monkeypatch.setattr(dropbox, "dropbox_rpc", lambda ep, cfg, body: {"name": {"display_name": "Acme"}})
    monkeypatch.setattr(dropbox, "dropbox_upload",
                        lambda cfg, path, content, **k: uploads.append(path))
    res = dropbox.Client({"token": "t", "folder": "Docs"}).push_reports(
        ARTS, metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"] and len(uploads) == 2 and uploads[0].startswith("/Docs/")


def test_dropbox_test(monkeypatch):
    monkeypatch.setattr(dropbox, "dropbox_rpc", lambda ep, cfg, body: {"name": {"display_name": "Acme"}})
    assert "Acme" in dropbox.Client({"token": "t"}).test()["detail"]


# --- Nuclino ---------------------------------------------------------------

def test_nuclino_creates(monkeypatch, bundle):
    calls = []
    monkeypatch.setattr(nuclino, "nuclino_request",
                        lambda m, p, cfg, **k: calls.append((m, p)) or {"data": {"url": "https://n/1"}})
    res = nuclino.Client({"api_key": "k", "workspace_id": "w"}).push_reports(
        ARTS, metrics=bundle_metrics(bundle), bundle=bundle)
    assert "Created" in res["detail"]
    assert ("POST", "/items") in calls


def test_nuclino_updates_existing(monkeypatch, bundle):
    calls = []
    monkeypatch.setattr(nuclino, "nuclino_request",
                        lambda m, p, cfg, **k: calls.append((m, p)) or {})
    res = nuclino.Client({"api_key": "k", "items": {"AcmeDB": "item1"}}).push_reports(
        ARTS, metrics=bundle_metrics(bundle), bundle=bundle)
    assert "Updated" in res["detail"]
    assert ("PUT", "/items/item1") in calls
