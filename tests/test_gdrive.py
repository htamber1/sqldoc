"""Google Drive connector tests — a fake Drive service is injected, so neither
the Google client nor the network is touched."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.integrations import gdrive
from sqldoc.integrations.base import Artifact, IntegrationError


CONFIG = {"service_account_file": "/x/sa.json", "folder_id": "FOLDER",
          "share_with": ["a@acme.com", "b@acme.com"]}


class _Exec:
    def __init__(self, value):
        self._v = value

    def execute(self):
        return self._v


class _Files:
    def __init__(self, service):
        self.s = service

    def list(self, **kw):
        # Return an existing file only for names the test pre-seeds.
        name = kw.get("q", "")
        for existing in self.s.existing:
            if f"name = '{existing}'" in name:
                return _Exec({"files": [{"id": f"ID-{existing}", "name": existing}]})
        return _Exec({"files": []})

    def get(self, **kw):
        return _Exec({"id": kw.get("fileId"), "name": "Reports"})

    def create(self, **kw):
        self.s.creates.append(kw)
        return _Exec({"id": "NEWID", "webViewLink": "https://drive/NEWID"})

    def update(self, **kw):
        self.s.updates.append(kw)
        return _Exec({"id": kw["fileId"], "webViewLink": f"https://drive/{kw['fileId']}"})


class _Perms:
    def __init__(self, service):
        self.s = service

    def create(self, **kw):
        self.s.perms.append(kw)
        return _Exec({"id": "perm"})


class _About:
    def get(self, **kw):
        return _Exec({"user": {"emailAddress": "sqldoc-sa@proj.iam.gserviceaccount.com"}})


class FakeService:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.creates, self.updates, self.perms = [], [], []

    def files(self):
        return _Files(self)

    def permissions(self):
        return _Perms(self)

    def about(self):
        return _About()


@pytest.fixture(autouse=True)
def no_media(monkeypatch):
    monkeypatch.setattr(gdrive, "_media_body", lambda art: f"<media:{art.name}>")


def test_test_ok(monkeypatch):
    monkeypatch.setattr(gdrive, "build_service", lambda cfg: FakeService())
    res = gdrive.Client(CONFIG).test()
    assert res["ok"] and "gserviceaccount" in res["detail"]


def test_missing_folder():
    with pytest.raises(IntegrationError):
        gdrive.Client({"service_account_file": "x"}).test()


def test_push_creates_and_shares(monkeypatch):
    svc = FakeService()
    monkeypatch.setattr(gdrive, "build_service", lambda cfg: svc)
    arts = [Artifact("db-doc.html", "doc_html", b"<html>", "text/html"),
            Artifact("db-exec.html", "executive_html", b"<html>", "text/html")]
    res = gdrive.Client(CONFIG).push_reports(arts)
    assert res["ok"] and res["uploaded"] == ["db-doc.html", "db-exec.html"]
    assert len(svc.creates) == 2 and not svc.updates
    # 2 files x 2 share recipients = 4 permission grants
    assert len(svc.perms) == 4
    assert res["url"] == "https://drive/NEWID"   # executive link preferred


def test_push_updates_existing_for_version_tracking(monkeypatch):
    svc = FakeService(existing={"db-doc.html"})
    monkeypatch.setattr(gdrive, "build_service", lambda cfg: svc)
    arts = [Artifact("db-doc.html", "doc_html", b"<html>", "text/html")]
    res = gdrive.Client(CONFIG).push_reports(arts)
    assert res["ok"]
    assert len(svc.updates) == 1 and not svc.creates
    assert svc.updates[0]["fileId"] == "ID-db-doc.html"


def test_push_nothing(monkeypatch):
    monkeypatch.setattr(gdrive, "build_service", lambda cfg: FakeService())
    with pytest.raises(IntegrationError):
        gdrive.Client(CONFIG).push_reports([])


def test_cli_test(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(gdrive, "build_service", lambda cfg: FakeService())
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"gdrive": CONFIG}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["gdrive", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "Google Drive" in res.output
