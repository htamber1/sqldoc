"""Foundation tests for the integration suite: the shared base helpers and the
collect-once `reports` module (gather -> artifacts / metrics / finding events).
No network and no live database — a tiny in-memory adapter drives it."""
import json

import pytest

from sqldoc.adapters.base import Capabilities
from sqldoc.integrations import base
from sqldoc.integrations import reports
from sqldoc.integrations.base import Artifact, FindingEvent, IntegrationError


class _ReportAdapter:
    """Adapter stand-in that serves fixture schema (metadata-only capabilities)."""
    def __init__(self, tables, views=None, procs=None, caps=None):
        self._t, self._v, self._p = tables, views or [], procs or []
        self.dialect = "sqlserver"
        self.display_name = "SQL Server (fake)"
        self.capabilities = caps or Capabilities()

    def extract_metadata(self):
        return list(self._t)

    def extract_views(self):
        return list(self._v)

    def extract_procedures(self):
        return list(self._p)


# --- base helpers ----------------------------------------------------------

def test_require_missing_dependency_names_extra():
    with pytest.raises(IntegrationError) as e:
        base.require("a_package_that_does_not_exist_xyz", "confluence")
    assert "pip install sqldoc[confluence]" in str(e.value)


def test_require_returns_present_module():
    assert base.require("json", "whatever") is json


def test_need_reports_all_missing_keys():
    with pytest.raises(IntegrationError) as e:
        base.need({"present": 1}, "present", "alpha", "beta", integration="jira")
    listed = str(e.value).split("key(s):")[1]
    assert "alpha" in listed and "beta" in listed and "present" not in listed


def test_need_returns_values_in_order():
    assert base.need({"x": 1, "y": 2}, "x", "y", integration="x") == (1, 2)


def test_artifact_text_and_repr():
    a = Artifact("f.html", "doc_html", b"<p>hi</p>", "text/html")
    assert a.text == "<p>hi</p>"
    assert "doc_html" in repr(a) and "9 bytes" in repr(a)


def test_result_envelope():
    r = base.result(True, "done", count=3)
    assert r == {"ok": True, "detail": "done", "count": 3}


# --- reports: gather / render / metrics / finding_events -------------------

def test_gather_metadata_only(sample_tables, sample_views, sample_procs):
    a = _ReportAdapter(sample_tables, sample_views, sample_procs)
    b = reports.gather(a, "TestDB")
    assert b.database == "TestDB"
    assert b.tables and b.views and b.procedures
    assert isinstance(b.findings, list)
    # No health/infra capability -> those stay empty, but nothing crashes.
    assert b.health_summary is None
    assert b.executive_summary is not None


def test_gather_schema_filter(sample_tables):
    a = _ReportAdapter(sample_tables)
    b = reports.gather(a, "TestDB", schemas="Sales")
    assert all(t.schema == "Sales" for t in b.tables)
    b2 = reports.gather(a, "TestDB", schemas="NoSuchSchema")
    assert b2.tables == []


def test_render_artifacts_default_kinds(sample_tables, sample_views, sample_procs):
    a = _ReportAdapter(sample_tables, sample_views, sample_procs)
    b = reports.gather(a, "Test DB!")
    arts = reports.render_artifacts(b)
    kinds = {x.kind for x in arts}
    # health_json is omitted (no health summary); the rest render.
    assert {"doc_html", "executive_html", "pii_html", "pii_json", "metrics_json"} <= kinds
    assert "health_json" not in kinds
    # Filenames are sanitised.
    assert all("!" not in x.name and " " not in x.name for x in arts)
    doc = next(x for x in arts if x.kind == "doc_html")
    assert b"<" in doc.content and doc.mime == "text/html"
    pj = next(x for x in arts if x.kind == "pii_json")
    parsed = json.loads(pj.text)
    assert parsed["database"] == "Test DB!" and "findings" in parsed


def test_render_artifacts_narrowed_kinds(sample_tables):
    a = _ReportAdapter(sample_tables)
    b = reports.gather(a, "TestDB")
    arts = reports.render_artifacts(b, kinds=["pii_json"])
    assert [x.kind for x in arts] == ["pii_json"]


def test_metrics_shape(sample_tables):
    a = _ReportAdapter(sample_tables)
    b = reports.gather(a, "TestDB")
    m = reports.metrics(b)
    assert m["database"] == "TestDB"
    assert m["tables"] == len(b.tables)
    assert m["pii_findings"] == len(b.findings)
    assert m["pii_high"] >= 0
    # Metadata-only dialect: infra scores are absent.
    assert m["security_score"] is None


def test_finding_events_pii_high(sample_tables):
    from sqldoc.pii import Finding
    a = _ReportAdapter(sample_tables)
    b = reports.gather(a, "TestDB")
    # Inject a HIGH finding so the pii event fires deterministically (the sample
    # schema has no naturally sensitive columns).
    b.findings.append(Finding(
        schema="Sales", table="Customer", column="SSN", data_type="varchar",
        category="government_id", risk="HIGH", confidence="name+type",
        regulations=["GDPR", "HIPAA"], action="Encrypt"))
    events = reports.finding_events(b)
    pii = [e for e in events if e.kind == "pii"]
    assert pii and pii[0].severity == "high"
    assert isinstance(pii[0], FindingEvent)
    assert "GDPR" in pii[0].detail


def test_finding_events_thresholds_no_scores(sample_tables):
    a = _ReportAdapter(sample_tables)
    b = reports.gather(a, "TestDB")
    # With no security/health scores available, threshold events don't fire.
    events = reports.finding_events(b, thresholds={"security_min": 90, "health_min": 90})
    assert not any(e.kind in ("security", "health") for e in events)
