"""Cover reports.gather error/capability branches + render_artifacts variants."""
import pytest

from sqldoc.adapters.base import Capabilities
from sqldoc.integrations import reports
from sqldoc.extractor import Table, Column


class Adapter:
    def __init__(self, tables, caps=None, views_exc=False, procs_exc=False):
        self._t = tables
        self.capabilities = caps or Capabilities()
        self.dialect = "sqlserver"
        self.display_name = "SQL Server"
        self._ve, self._pe = views_exc, procs_exc

    def extract_metadata(self):
        return list(self._t)

    def extract_views(self):
        if self._ve:
            raise RuntimeError("views boom")
        return []

    def extract_procedures(self):
        if self._pe:
            raise RuntimeError("procs boom")
        return []


def _tables():
    return [Table("Sales", "Customer", 3, [Column("Email", "varchar", 50, True, False, False, None, None)])]


def test_gather_records_view_proc_errors():
    b = reports.gather(Adapter(_tables(), views_exc=True, procs_exc=True), "DB")
    assert any("views not collected" in n for n in b.notes)
    assert any("procedures not collected" in n for n in b.notes)


def test_gather_health_infra_capabilities(monkeypatch):
    caps = Capabilities(health=True, infra_monitoring=True)
    monkeypatch.setattr("sqldoc.health.collect_health", lambda a: object())
    monkeypatch.setattr("sqldoc.health.summarize", lambda r: {"issues": 3, "slow_queries": 1,
                                                              "missing_indexes": 2})
    monkeypatch.setattr("sqldoc.backup.collect_backups", lambda a: object())
    monkeypatch.setattr("sqldoc.secure.collect_security", lambda a: object())
    b = reports.gather(Adapter(_tables(), caps=caps), "DB")
    assert b.health_summary == {"issues": 3, "slow_queries": 1, "missing_indexes": 2}
    assert b.backup_report is not None and b.security_report is not None


def test_gather_health_failure_noted(monkeypatch):
    caps = Capabilities(health=True)
    monkeypatch.setattr("sqldoc.health.collect_health", lambda a: (_ for _ in ()).throw(RuntimeError("dmv")))
    b = reports.gather(Adapter(_tables(), caps=caps), "DB")
    assert any("health skipped" in n for n in b.notes) and b.health_summary is None


def test_render_artifacts_with_health(monkeypatch):
    b = reports.gather(Adapter(_tables()), "DB")
    b.health_summary = {"issues": 2, "slow_queries": 0, "missing_indexes": 1}
    arts = reports.render_artifacts(b, kinds=["health_json", "metrics_json"])
    kinds = {a.kind for a in arts}
    assert "health_json" in kinds and "metrics_json" in kinds


def test_metrics_all_none():
    b = reports.gather(Adapter(_tables()), "DB")
    m = reports.metrics(b)
    assert m["security_score"] is None and m["health_score"] is None and m["database"] == "DB"


def test_finding_events_backup_and_thresholds():
    b = reports.gather(Adapter(_tables()), "DB")

    class Summary:
        security_score = 50
        security_grade = "F"
        health_score = 40
        backup_compliance_pct = 30
    b.executive_summary = Summary()
    events = reports.finding_events(b, {"security_min": 80, "health_min": 70})
    kinds = {e.kind for e in events}
    assert {"security", "health", "backup"} <= kinds
    backup = next(e for e in events if e.kind == "backup")
    assert backup.severity == "high"        # <50% coverage -> high
