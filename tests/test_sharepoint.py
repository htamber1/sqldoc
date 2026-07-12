"""SharePoint connector tests — no MSAL, no Graph, no network. The token
acquisition and every HTTP call are module-level functions we monkeypatch."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.integrations import sharepoint
from sqldoc.integrations.base import Artifact, IntegrationError


CONFIG = {
    "tenant_id": "t", "client_id": "c", "client_secret": "s",
    "site_id": "acme.sharepoint.com,siteguid,webguid",
    "folder": "DB Docs", "list_name": "Database Documentation",
}


@pytest.fixture
def graph_recorder(monkeypatch):
    """Record every graph_request and return canned responses by (method, url)."""
    calls = []

    def fake_token(config):
        assert config["client_id"] == "c"
        return "TESTTOKEN"

    def fake_graph(method, url, token, **kwargs):
        assert token == "TESTTOKEN"
        calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/sites/" + CONFIG["site_id"]):
            return {"displayName": "Acme DB Site"}
        if method == "GET" and url.endswith("/lists"):
            return {"value": [{"id": "LIST123", "displayName": "Database Documentation"}]}
        if method == "PUT":
            return {"webUrl": "https://acme.sharepoint.com/" + url.rsplit("/", 2)[-2]}
        if method == "POST":
            return {"id": "ITEM1"}
        return {}

    monkeypatch.setattr(sharepoint, "acquire_token", fake_token)
    monkeypatch.setattr(sharepoint, "graph_request", fake_graph)
    return calls


def test_test_returns_site_name(graph_recorder):
    res = sharepoint.Client(CONFIG).test()
    assert res["ok"] and "Acme DB Site" in res["detail"]


def test_missing_config_raises():
    with pytest.raises(IntegrationError) as e:
        sharepoint.Client({"tenant_id": "t"}).test()
    assert "client_id" in str(e.value)


def test_push_uploads_files_and_list_row(graph_recorder):
    arts = [
        Artifact("db-doc.html", "doc_html", b"<html>", "text/html"),
        Artifact("db-exec.html", "executive_html", b"<html>", "text/html"),
        Artifact("db-pii.json", "pii_json", b"{}", "application/json"),
    ]
    metrics = {"database": "DB", "pii_high": 2, "security_score": 80,
               "health_score": None, "overall_score": 75, "backup_compliance_pct": 100,
               "pii_findings": 5}
    res = sharepoint.Client(CONFIG).push_reports(arts, metrics=metrics)
    assert res["ok"]
    assert res["uploaded"] == ["db-doc.html", "db-exec.html", "db-pii.json"]
    puts = [c for c in graph_recorder if c[0] == "PUT"]
    posts = [c for c in graph_recorder if c[0] == "POST"]
    assert len(puts) == 3          # one upload per artifact
    assert len(posts) == 1         # one summary list row
    # Null metrics (health_score) are dropped from the list fields.
    row_fields = posts[0][2]["json"]["fields"]
    assert "HealthScore" not in row_fields
    assert row_fields["SecurityScore"] == 80
    assert row_fields["Database"] == "DB"


def test_push_list_not_found(monkeypatch):
    def fake_graph(method, url, token, **kwargs):
        if url.endswith("/lists"):
            return {"value": []}    # no matching list
        return {"webUrl": "x"}
    monkeypatch.setattr(sharepoint, "acquire_token", lambda c: "TESTTOKEN")
    monkeypatch.setattr(sharepoint, "graph_request", fake_graph)
    arts = [Artifact("a.html", "doc_html", b"<html>", "text/html")]
    with pytest.raises(IntegrationError) as e:
        sharepoint.Client(CONFIG).push_reports(arts, metrics={"database": "DB"})
    assert "not found" in str(e.value)


def test_push_nothing_to_upload(graph_recorder):
    with pytest.raises(IntegrationError):
        sharepoint.Client(CONFIG).push_reports([], metrics={})


# --- CLI wiring ------------------------------------------------------------

def _write_config(tmp_path):
    import yaml
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"sharepoint": CONFIG}), encoding="utf-8")
    return str(p)


def test_cli_test_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(sharepoint, "acquire_token", lambda c: "TESTTOKEN")
    monkeypatch.setattr(sharepoint, "graph_request",
                        lambda m, u, t, **k: {"displayName": "Acme DB Site"})
    res = CliRunner().invoke(cli.cli, ["sharepoint", "--config", _write_config(tmp_path), "--test"])
    assert res.exit_code == 0, res.output
    assert "Acme DB Site" in res.output


def test_cli_requires_a_flag(tmp_path):
    res = CliRunner().invoke(cli.cli, ["sharepoint", "--config", _write_config(tmp_path)])
    assert res.exit_code != 0
    assert "--test" in res.output and "--push" in res.output


def test_cli_test_failure_exits_nonzero(monkeypatch, tmp_path):
    def boom(config):
        raise IntegrationError("SharePoint auth failed: bad secret")
    monkeypatch.setattr(sharepoint, "acquire_token", boom)
    res = CliRunner().invoke(cli.cli, ["sharepoint", "--config", _write_config(tmp_path), "--test"])
    assert res.exit_code == 1
    assert "auth failed" in res.output
