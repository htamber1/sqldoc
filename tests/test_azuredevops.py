"""Azure DevOps connector tests — ado_request monkeypatched, no network."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.adapters.base import Capabilities
from sqldoc.integrations import azuredevops as ado
from sqldoc.integrations.base import Artifact, IntegrationError
from sqldoc.integrations.reports import gather, metrics as bundle_metrics


CONFIG = {"organization": "acme", "project": "Data", "pat": "tok",
          "work_item_type": "Issue", "doc_work_item_type": "Task"}


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


def make_router(existing_doc_item=None):
    calls = []
    counter = {"n": 100}

    def router(method, url, cfg, **kwargs):
        calls.append((method, url, kwargs))
        if "/_apis/projects/" in url:
            return {"name": "Data"}
        if "/wiql?" in url:
            title = kwargs["json"]["query"]
            if existing_doc_item and "Database documentation" in title:
                return {"workItems": [{"id": existing_doc_item}]}
            return {"workItems": []}
        if "/wit/attachments?" in url:
            return {"id": "att", "url": "https://ado/att/1"}
        if "/wit/workitems/$" in url and method == "POST":
            counter["n"] += 1
            return {"id": counter["n"]}
        if "/wit/workitems/" in url and method == "PATCH":
            return {"id": url.split("/workitems/")[1].split("?")[0]}
        return {}

    router.calls = calls
    return router


def test_work_item_type_map():
    assert ado.work_item_type_for(CONFIG, "health") == "Issue"   # single override applies
    assert ado.work_item_type_for({}, "health") == "Bug"          # default
    assert ado.work_item_type_for({"work_item_type_map": {"pii": "Epic"}}, "pii") == "Epic"


def test_test_ok(monkeypatch):
    monkeypatch.setattr(ado, "ado_request", make_router())
    res = ado.Client(CONFIG).test()
    assert res["ok"] and "Data" in res["detail"]


def test_push_attaches_reports_and_creates_findings(monkeypatch, bundle):
    router = make_router()
    monkeypatch.setattr(ado, "ado_request", router)
    arts = [Artifact("AcmeDB-doc.html", "doc_html", b"<html>", "text/html"),
            Artifact("AcmeDB-pii.json", "pii_json", b"{}", "application/json")]
    res = ado.Client(CONFIG).push_reports(arts, metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"]
    attaches = [c for c in router.calls if "/wit/attachments?" in c[1]]
    assert len(attaches) == 2       # one per report
    # doc item created + at least one finding work item (HIGH PII)
    creates = [c for c in router.calls if "/wit/workitems/$" in c[1] and c[0] == "POST"]
    assert len(creates) >= 2
    assert res["created"]           # PII finding item created


def test_push_reuses_existing_doc_item(monkeypatch, bundle):
    router = make_router(existing_doc_item=55)
    monkeypatch.setattr(ado, "ado_request", router)
    arts = [Artifact("AcmeDB-doc.html", "doc_html", b"<html>", "text/html")]
    res = ado.Client(CONFIG).push_reports(arts, metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["work_item"] == 55
    # attachment linked to the existing item, not a new one
    patches = [c for c in router.calls if c[0] == "PATCH" and "/workitems/55?" in c[1]]
    assert patches


def test_missing_config():
    with pytest.raises(IntegrationError):
        ado.Client({"project": "x"}).test()


def test_cli_test(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(ado, "ado_request", make_router())
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"azuredevops": CONFIG}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["azuredevops", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "Azure DevOps" in res.output
