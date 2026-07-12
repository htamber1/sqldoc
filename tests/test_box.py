"""Box connector tests — a fake Box client is injected (no SDK, no network)."""
import pytest
from click.testing import CliRunner

from sqldoc import cli
from sqldoc.integrations import box
from sqldoc.integrations.base import Artifact, IntegrationError


CONFIG = {"jwt_config_file": "/x/box.json", "folder_id": "999",
          "shared_link_access": "company", "database": "MyDB"}


class _Item:
    def __init__(self, id, name, type="file"):
        self.id, self.name, self.type = id, name, type


class _BoxFile:
    def __init__(self, id, service):
        self.id, self.s = id, service

    def update_contents_stream(self, stream):
        self.s.updates.append(self.id)
        return self

    def get_shared_link(self, access="company"):
        self.s.links.append((self.id, access))
        return f"https://box/{self.id}?access={access}"

    def metadata(self, scope, template):
        return _Metadata(self.id, self.s)


class _Metadata:
    def __init__(self, id, service):
        self.id, self.s = id, service

    def create(self, values):
        self.s.metadata.append((self.id, values))
        return values


class _Folder:
    def __init__(self, id, service):
        self.id, self.s = id, service
        self.name = "Reports"

    def get(self):
        return self

    def get_items(self):
        return self.s.items

    def upload_stream(self, stream, name):
        self.s.creates.append(name)
        return _BoxFile(f"NEW-{name}", self.s)


class _User:
    def get(self):
        u = _Item("1", "SA")
        u.name = "sqldoc Service Account"
        return u


class FakeBox:
    def __init__(self, items=()):
        self.items = list(items)
        self.creates, self.updates, self.links, self.metadata = [], [], [], []

    def folder(self, folder_id):
        return _Folder(folder_id, self)

    def file(self, file_id):
        return _BoxFile(file_id, self)

    def user(self):
        return _User()


def test_test_ok(monkeypatch):
    monkeypatch.setattr(box, "build_client", lambda cfg: FakeBox())
    res = box.Client(CONFIG).test()
    assert res["ok"] and "Service Account" in res["detail"]


def test_missing_folder():
    with pytest.raises(IntegrationError):
        box.Client({"jwt_config_file": "x"}).test()


def test_push_creates_tags_and_links(monkeypatch):
    fb = FakeBox()
    monkeypatch.setattr(box, "build_client", lambda cfg: fb)
    arts = [Artifact("db-doc.html", "doc_html", b"<html>", "text/html"),
            Artifact("db-exec.html", "executive_html", b"<html>", "text/html")]
    res = box.Client(CONFIG).push_reports(arts)
    assert res["ok"] and len(fb.creates) == 2
    # both files tagged with database + scan_date metadata
    assert len(fb.metadata) == 2
    assert all(v["database"] == "MyDB" and "scan_date" in v for _id, v in fb.metadata)
    # shared links created at the configured access level
    assert all(acc == "company" for _id, acc in fb.links)
    assert res["url"].startswith("https://box/")


def test_push_updates_existing(monkeypatch):
    fb = FakeBox(items=[_Item("EXIST1", "db-doc.html")])
    monkeypatch.setattr(box, "build_client", lambda cfg: fb)
    arts = [Artifact("db-doc.html", "doc_html", b"<html>", "text/html")]
    res = box.Client(CONFIG).push_reports(arts)
    assert res["ok"]
    assert fb.updates == ["EXIST1"] and not fb.creates


def test_push_nothing(monkeypatch):
    monkeypatch.setattr(box, "build_client", lambda cfg: FakeBox())
    with pytest.raises(IntegrationError):
        box.Client(CONFIG).push_reports([])


def test_metadata_conflict_is_non_fatal(monkeypatch):
    fb = FakeBox()

    class _MdBoom(_Metadata):
        def create(self, values):
            raise RuntimeError("409 conflict")

        def start_update(self):
            raise RuntimeError("no update either")

    def file(file_id):
        f = _BoxFile(file_id, fb)
        f.metadata = lambda s, t: _MdBoom(file_id, fb)
        return f
    fb.file = file
    monkeypatch.setattr(box, "build_client", lambda cfg: fb)
    arts = [Artifact("db-doc.html", "doc_html", b"<html>", "text/html")]
    # metadata failure must not break the upload
    res = box.Client(CONFIG).push_reports(arts)
    assert res["ok"]


def test_cli_test(monkeypatch, tmp_path):
    import yaml
    monkeypatch.setattr(box, "build_client", lambda cfg: FakeBox())
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump({"box": CONFIG}), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["box", "--config", str(p), "--test"])
    assert res.exit_code == 0, res.output
    assert "Box" in res.output
