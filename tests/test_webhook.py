"""Generic webhook connector tests — transport monkeypatched, no network."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.adapters.base import Capabilities
from sqldoc.integrations import webhook
from sqldoc.integrations.base import Artifact, IntegrationError
from sqldoc.integrations.reports import gather, metrics as bundle_metrics


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
    return gather(_Adapter(sample_tables), "AcmeDB")


@pytest.fixture
def recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(webhook, "post",
                        lambda url, method, headers, payload, timeout=30.0: calls.append(
                            (url, method, headers, payload)))
    return calls


def test_render_template_substitutes():
    ctx = {"database": "DB", "overall_score": 78, "pii_high": 2}
    tmpl = {"event": "scan", "db": "{database}", "score": "{overall_score}",
            "nested": ["{pii_high} high", {"x": "{missing}"}]}
    out = webhook.render_template(tmpl, ctx)
    assert out["db"] == "DB" and out["score"] == "78"
    assert out["nested"][0] == "2 high"
    assert out["nested"][1]["x"] == ""     # missing -> empty


def test_test_sends_ping(recorder):
    webhook.Client({"url": "https://hook"}).test()
    assert recorder[0][0] == "https://hook"
    assert recorder[0][3]["event"] == "sqldoc.test"


def test_push_default_payload(recorder, bundle):
    res = webhook.Client({"url": "https://hook"}).push_reports(
        [Artifact("a.html", "doc_html", b"<html>", "text/html")],
        metrics=bundle_metrics(bundle), bundle=bundle)
    assert res["ok"] and "default" in res["detail"]
    payload = recorder[0][3]
    assert payload["event"] == "sqldoc.report"
    assert payload["database"] == "AcmeDB"
    assert payload["artifacts"] == ["a.html"]
    assert "pii_summary" in payload


def test_push_custom_template(recorder, bundle):
    cfg = {"url": "https://hook", "method": "PUT",
           "headers": {"Authorization": "Bearer x"},
           "payload_template": {"event": "database.scanned", "name": "{database}",
                                "risk": "{overall_score}"}}
    res = webhook.Client(cfg).push_reports([], metrics=bundle_metrics(bundle), bundle=bundle)
    assert "custom" in res["detail"]
    url, method, headers, payload = recorder[0]
    assert method == "PUT" and headers["Authorization"] == "Bearer x"
    assert payload["event"] == "database.scanned" and payload["name"] == "AcmeDB"


def test_missing_url():
    with pytest.raises(IntegrationError):
        webhook.Client({}).test()


def test_cli_test(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(webhook, "post", lambda *a, **k: None)
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"webhook": {"url": "https://hook"}}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["webhook", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "accepted a test payload" in res.output
