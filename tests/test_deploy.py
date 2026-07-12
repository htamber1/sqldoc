"""Sanity checks for the container + Helm deployment assets."""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_dockerfile_runs_agent():
    df = _read("Dockerfile")
    assert 'ENTRYPOINT ["sqldoc"]' in df
    assert '"agent", "start", "--foreground"' in df
    assert "SQLDOC_AGENT_HOME=/data" in df
    assert "msodbcsql18" in df          # SQL Server ODBC driver bundled


def test_compose_valid_and_persistent():
    doc = yaml.safe_load(_read("docker-compose.yml"))
    svc = doc["services"]["sqldoc"]
    assert svc["ports"] == ["8080:8080"]
    assert any("sqldoc-data:/data" in v for v in svc["volumes"])
    assert "sqldoc-data" in doc["volumes"]


def test_helm_chart_metadata():
    chart = yaml.safe_load(_read("helm", "sqldoc", "Chart.yaml"))
    assert chart["name"] == "sqldoc" and chart["apiVersion"] == "v2"
    assert "appVersion" in chart


def test_helm_values_parse():
    values = yaml.safe_load(_read("helm", "sqldoc", "values.yaml"))
    assert values["persistence"]["enabled"] is True
    assert values["service"]["port"] == 8080
    assert values["securityContext"]["runAsNonRoot"] is True


def test_helm_templates_present():
    tdir = os.path.join(ROOT, "helm", "sqldoc", "templates")
    for f in ("deployment.yaml", "service.yaml", "pvc.yaml", "secret.yaml", "_helpers.tpl"):
        assert os.path.exists(os.path.join(tdir, f)), f


def test_deploy_readme_covers_all_three():
    readme = _read("deploy", "README.md")
    assert "Docker" in readme and "Docker Compose" in readme and "Helm" in readme
