"""Role recommender: peer gathering, least-privilege recommendation, render."""
import pytest

from sqldoc.access import recommend as rec_mod
from sqldoc.access.recommend import gather_peers, recommend_roles, PeerProfile
from sqldoc.access.model import ADUser
from sqldoc.access.render import build_recommend_json, render_recommend_html


class FakeCursor:
    def __init__(self, data):
        self.data, self._rows = data, []

    def execute(self, sql, *a):
        self._rows = []
        for token, rows in self.data.items():
            if token in sql:
                self._rows = rows
                return

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, data):
        self.data = data

    def cursor(self):
        return FakeCursor(self.data)

    def close(self):
        pass


class FakeAdapter:
    def __init__(self, data):
        self._data = data

    def connect(self):
        return FakeConn(self._data)

    def cursor(self, conn):
        return conn.cursor()


DATA = {
    "ACCESS_DB_PRINCIPALS": [
        {"db_user": "CORP\\peer1", "type_desc": "WINDOWS_USER"},
        {"db_user": "CORP\\peer2", "type_desc": "WINDOWS_USER"},
        {"db_user": "svc_x", "type_desc": "SQL_USER"},
    ],
    "ACCESS_DB_ROLE_MEMBERS": [
        {"role_name": "db_datareader", "member_name": "CORP\\peer1"},
        {"role_name": "db_datareader", "member_name": "CORP\\peer2"},
        {"role_name": "db_datawriter", "member_name": "CORP\\peer1"},
        {"role_name": "db_owner", "member_name": "CORP\\peer2"},   # above title -> should be capped out
    ],
}


class FakeSource:
    TITLES = {"peer1": ("Data Analyst", "Sales"),
              "peer2": ("Data Analyst", "Sales")}

    def get_user(self, name):
        part = name.split("\\")[-1].lower()
        if part in self.TITLES:
            t, d = self.TITLES[part]
            return ADUser(identifier=name, found=True, title=t, department=d)
        return ADUser(identifier=name, found=False)


def _cfg():
    return {"access": {"ad": {"type": "ldap", "server": "x", "base_dn": "y"},
                       "servers": [{"name": "prod", "connection_string": "c",
                                    "dialect": "sqlserver", "databases": ["Sales"]}]}}


# --- gather_peers ----------------------------------------------------------

def test_gather_peers():
    peers = gather_peers(_cfg(), FakeSource(), adapter_factory=lambda e, d: FakeAdapter(DATA))
    assert len(peers) == 2                       # only resolvable windows users
    p1 = next(p for p in peers if p.login == "CORP\\peer1")
    assert p1.title == "Data Analyst" and "db_datareader" in p1.roles


# --- recommend_roles -------------------------------------------------------

def _analyst():
    return ADUser(identifier="newhire", display_name="New Hire", title="Sales Analyst",
                  department="Sales", found=True)


def test_recommend_least_privilege_caps_admin():
    peers = [
        PeerProfile("CORP\\p1", "Data Analyst", "Sales", "Sales", ["db_datareader"]),
        PeerProfile("CORP\\p2", "Data Analyst", "Sales", "Sales", ["db_datareader", "db_owner"]),
    ]
    rec = recommend_roles(_analyst(), peers, database="Sales", no_ai=True)
    roles = [r for r, _ in rec.recommended_roles]
    assert "db_datareader" in roles          # baseline + peer-common
    assert "db_owner" not in roles           # capped: title = read level
    assert rec.peers_considered == 2


def test_recommend_adds_peer_common_within_level():
    # writer-title peers who commonly hold db_datawriter
    peers = [
        PeerProfile("CORP\\e1", "Data Engineer", "Data", "Sales", ["db_datareader", "db_datawriter"]),
        PeerProfile("CORP\\e2", "Data Engineer", "Data", "Sales", ["db_datareader", "db_datawriter"]),
    ]
    user = ADUser(identifier="eng", title="Data Engineer", department="Data", found=True)
    rec = recommend_roles(user, peers, database="Sales", no_ai=True)
    roles = [r for r, _ in rec.recommended_roles]
    assert "db_datareader" in roles and "db_datawriter" in roles


def test_recommend_no_peers_uses_baseline():
    rec = recommend_roles(_analyst(), [], no_ai=True)
    roles = [r for r, _ in rec.recommended_roles]
    assert roles == ["db_datareader"] and rec.peers_considered == 0


def test_recommend_ai_rationale(monkeypatch):
    import sqldoc.ai as real_ai
    monkeypatch.setattr(real_ai, "dispatch", lambda *a, **k: "Least-privilege read access fits an analyst.")
    peers = [PeerProfile("CORP\\p1", "Data Analyst", "Sales", "Sales", ["db_datareader"])]
    rec = recommend_roles(_analyst(), peers, database="Sales")
    assert "analyst" in rec.rationale.lower()


# --- render ----------------------------------------------------------------

def test_build_recommend_json():
    rec = recommend_roles(_analyst(), [], no_ai=True)
    j = build_recommend_json(rec)
    assert j["report_type"] == "access-role-recommendation"
    assert j["recommended_roles"][0]["role"] == "db_datareader"


def test_render_recommend_html_offline(tmp_path):
    from sqldoc.offline import verify_file
    rec = recommend_roles(_analyst(), [
        PeerProfile("CORP\\p1", "Data Analyst", "Sales", "Sales", ["db_datareader"])], no_ai=True)
    out = tmp_path / "rec.html"
    render_recommend_html(rec, str(out))
    text = out.read_text(encoding="utf-8")
    assert "Role recommendation" in text and "db_datareader" in text
    assert verify_file(str(out)) == []


# --- CLI -------------------------------------------------------------------

def test_cli_access_recommend(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.access import ad as ad_mod
    monkeypatch.setattr(ad_mod, "get_source", lambda cfg: FakeSource2())
    monkeypatch.setattr(rec_mod, "gather_peers",
                        lambda cfg, source, **k: [PeerProfile("CORP\\p1", "Sales Analyst",
                                                              "Sales", "Sales", ["db_datareader"])])
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(_cfg()), encoding="utf-8")
    res = CliRunner().invoke(cli.cli, ["access", "recommend", "--config", str(p),
                                       "--user", "newhire", "--database", "Sales", "--no-ai",
                                       "--output", str(tmp_path / "rec.html")])
    assert res.exit_code == 0, res.output
    assert "db_datareader" in res.output


class FakeSource2:
    def get_user(self, ident):
        return _analyst()
