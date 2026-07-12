"""Power BI connector tests — transport monkeypatched, no MSAL, no network."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.integrations import powerbi
from sqldoc.integrations.base import IntegrationError


PUSH_URL_CFG = {"push_url": "https://api.powerbi.com/beta/ws/datasets/id/rows?key=abc"}
AAD_CFG = {"tenant_id": "t", "client_id": "c", "client_secret": "s",
           "group_id": "G", "dataset_id": "D"}
METRICS = {"database": "DB", "pii_high": 2, "pii_findings": 5, "security_score": 80,
           "health_score": None, "backup_compliance_pct": 100, "overall_score": 78}


def test_row_drops_nulls():
    row = powerbi._row(METRICS)
    assert "health_score" not in row       # None dropped
    assert row["security_score"] == 80
    assert row["database"] == "DB" and row["timestamp"].endswith("Z")


def test_push_url_test(monkeypatch):
    posted = []
    monkeypatch.setattr(powerbi, "post_rows_url", lambda url, rows, **k: posted.append((url, rows)))
    res = powerbi.Client(PUSH_URL_CFG).test()
    assert res["ok"] and posted[0][1] == []


def test_push_url_push(monkeypatch):
    posted = []
    monkeypatch.setattr(powerbi, "post_rows_url", lambda url, rows, **k: posted.append((url, rows)))
    res = powerbi.Client(PUSH_URL_CFG).push_metrics(METRICS)
    assert res["ok"]
    assert posted[0][0] == PUSH_URL_CFG["push_url"]
    assert posted[0][1][0]["database"] == "DB"


def test_aad_test(monkeypatch):
    monkeypatch.setattr(powerbi, "acquire_token", lambda cfg: "TOKEN")
    monkeypatch.setattr(powerbi, "api_request",
                        lambda m, u, t, **k: {"name": "sqldoc-metrics"})
    res = powerbi.Client(AAD_CFG).test()
    assert res["ok"] and "sqldoc-metrics" in res["detail"]


def test_aad_push(monkeypatch):
    calls = []
    monkeypatch.setattr(powerbi, "acquire_token", lambda cfg: "TOKEN")

    def fake_api(method, url, token, **kwargs):
        calls.append((method, url, kwargs))
        return {}
    monkeypatch.setattr(powerbi, "api_request", fake_api)
    res = powerbi.Client(AAD_CFG).push_metrics(METRICS)
    assert res["ok"]
    post = calls[0]
    assert post[0] == "POST" and "/datasets/D/rows" in post[1]
    assert post[2]["json"]["rows"][0]["database"] == "DB"


def test_aad_missing_config():
    with pytest.raises(IntegrationError):
        powerbi.Client({"tenant_id": "t"}).test()


def test_cli_test_push_url(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(powerbi, "post_rows_url", lambda url, rows, **k: None)
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"powerbi": PUSH_URL_CFG}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["powerbi", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "streaming dataset" in res.output
