"""Agent config parsing + notification dispatch."""
import pytest

from sqldoc.agent import config as agentcfg
from sqldoc.agent import notify as notifymod
from sqldoc.agent.config import parse_agent_config, NotifyConfig
from sqldoc.agent.notify import Notifier


def test_parse_minimal_connection_string():
    cfg = {"agent": {"databases": [
        {"name": "prod", "connection_string": "postgresql://u:p@h/db"}]}}
    ac = parse_agent_config(cfg)
    assert ac.interval_minutes == 30 and ac.dashboard_port == 8080
    assert len(ac.databases) == 1
    db = ac.databases[0]
    assert db.name == "prod" and db.dialect == "postgres"
    assert db.connection_string == "postgresql://u:p@h/db"


def test_parse_builds_connection_string_from_parts():
    cfg = {"agent": {"databases": [
        {"name": "wh", "dialect": "sqlserver", "server": "h", "database": "DB",
         "username": "sa", "password": "x"}]}}
    db = parse_agent_config(cfg).databases[0]
    assert "ODBC Driver 18 for SQL Server" in db.connection_string
    assert "SERVER=h" in db.connection_string and db.dialect == "sqlserver"


def test_parse_overrides_and_notifications():
    cfg = {"agent": {
        "interval_minutes": 5, "dashboard_port": 9000, "mode": "cloud",
        "databases": [{"name": "a", "connection_string": "mysql://u:p@h/db"}],
        "notifications": {
            "slack_webhook": "https://hooks.slack.com/x",
            "email": {"smtp_host": "smtp.x", "to": ["dba@x"]},
            "on": ["schema_change", "new_pii"],
        },
    }}
    ac = parse_agent_config(cfg)
    assert ac.interval_minutes == 5 and ac.dashboard_port == 9000
    assert ac.databases[0].dialect == "mysql"
    assert ac.notify.slack_webhook == "https://hooks.slack.com/x"
    assert ac.notify.on == ["schema_change", "new_pii"]


def test_parse_server_monitoring_options():
    cfg = {"agent": {
        "databases": [{"name": "a", "connection_string": "Driver=x;Server=s;Database=d;"}],
        "server_monitoring": True,
        "disk_threshold_percent": 15,
        "errorlog_severity": 19,
        "notifications": {"on": ["job_failure", "disk_low", "errorlog_critical",
                                 "linked_server_down"]},
    }}
    ac = parse_agent_config(cfg)
    assert ac.server_monitoring is True
    assert ac.disk_threshold_percent == 15.0
    assert ac.errorlog_severity == 19
    assert "linked_server_down" in ac.notify.on


def test_server_monitoring_defaults_off():
    cfg = {"agent": {"databases": [{"name": "a", "connection_string": "x"}]}}
    ac = parse_agent_config(cfg)
    assert ac.server_monitoring is False and ac.disk_threshold_percent == 10.0
    assert ac.backup_monitoring is False and ac.ha_monitoring is False


def test_parse_backup_and_ha_options():
    cfg = {"agent": {
        "databases": [{"name": "a", "connection_string": "postgresql://u:p@h/db"}],
        "backup_monitoring": True, "backup_max_age_hours": 12,
        "ha_monitoring": True, "replica_lag_threshold_seconds": 60,
        "notifications": {"on": ["backup_stale", "replica_lag"]},
    }}
    ac = parse_agent_config(cfg)
    assert ac.backup_monitoring and ac.backup_max_age_hours == 12.0
    assert ac.ha_monitoring and ac.replica_lag_threshold_seconds == 60.0
    assert "replica_lag" in ac.notify.on


def test_parse_tempdb_threshold():
    cfg = {"agent": {"databases": [{"name": "a", "connection_string": "x"}],
                     "tempdb_version_store_mb": 4096}}
    ac = parse_agent_config(cfg)
    assert ac.tempdb_version_store_mb == 4096.0


@pytest.mark.parametrize("cfg, msg", [
    ({}, "No 'agent:'"),
    ({"agent": {"databases": []}}, "non-empty list"),
    ({"agent": {"databases": [{"connection_string": "x"}]}}, "needs a 'name'"),
    ({"agent": {"databases": [{"name": "a"}]}}, "connection_string"),
    ({"agent": {"databases": [{"name": "a", "connection_string": "x"},
                              {"name": "a", "connection_string": "y"}]}}, "duplicate"),
    ({"agent": {"interval_minutes": 0,
                "databases": [{"name": "a", "connection_string": "x"}]}}, "at least 1"),
    ({"agent": {"databases": [{"name": "a", "connection_string": "x"}],
                "notifications": {"on": ["bogus"]}}}, "unknown notification"),
])
def test_parse_errors(cfg, msg):
    with pytest.raises(ValueError) as ei:
        parse_agent_config(cfg)
    assert msg in str(ei.value)


# --- notifier --------------------------------------------------------------

def test_notifier_respects_allowlist():
    n = Notifier(NotifyConfig(on=["schema_change"]))
    assert n.should_notify("schema_change") is True
    assert n.should_notify("new_pii") is False
    assert n.notify("new_pii", "x", "y") == []      # filtered -> no channels


def test_notifier_dispatches_slack_and_email(monkeypatch):
    sent = []
    monkeypatch.setattr(notifymod, "send_slack", lambda wh, text: sent.append(("slack", wh, text)))
    monkeypatch.setattr(notifymod, "send_email", lambda smtp, subj, body: sent.append(("email", subj, body)))
    n = Notifier(NotifyConfig(slack_webhook="https://hook", smtp={"smtp_host": "x", "to": ["a"]}))
    results = n.notify("schema_change", "Schema changed", "2 tables added")
    assert ("slack", True, None) in results and ("email", True, None) in results
    assert sent[0][0] == "slack" and "Schema changed" in sent[0][2]


def test_notifier_isolates_channel_failures(monkeypatch):
    def boom_slack(wh, text):
        raise RuntimeError("network down")
    monkeypatch.setattr(notifymod, "send_slack", boom_slack)
    n = Notifier(NotifyConfig(slack_webhook="https://hook"))
    results = n.notify("schema_change", "x", "y")
    assert results == [("slack", False, "RuntimeError: network down")]


def test_notifier_dispatches_teams(monkeypatch):
    sent = []
    monkeypatch.setattr(notifymod, "send_teams",
                        lambda wh, title, text, color="0076D7": sent.append((wh, title, text, color)))
    n = Notifier(NotifyConfig(teams_webhook="https://outlook.office.com/webhook/x"))
    results = n.notify("doc_updated", "Docs published", "SharePoint updated")
    assert ("teams", True, None) in results
    # doc updates get the green theme colour
    assert sent[0][3] == "2EB67D" and "Docs published" in sent[0][1]


def test_parse_teams_webhook():
    cfg = {"agent": {
        "databases": [{"name": "a", "connection_string": "mysql://u:p@h/db"}],
        "notifications": {"teams_webhook": "https://outlook.office.com/webhook/y"},
    }}
    ac = parse_agent_config(cfg)
    assert ac.notify.teams_webhook == "https://outlook.office.com/webhook/y"


def test_notifier_dispatches_webex(monkeypatch):
    sent = []
    monkeypatch.setattr(notifymod, "send_webex",
                        lambda cfg, title, text: sent.append((cfg, title, text)))
    n = Notifier(NotifyConfig(webex={"token": "T", "room_id": "R"}))
    results = n.notify("disk_low", "Disk low", "5% free")
    assert ("webex", True, None) in results
    assert sent[0][0] == {"token": "T", "room_id": "R"}


def test_parse_webex():
    cfg = {"agent": {
        "databases": [{"name": "a", "connection_string": "mysql://u:p@h/db"}],
        "notifications": {"webex": {"token": "T", "room_id": "R"}},
    }}
    ac = parse_agent_config(cfg)
    assert ac.notify.webex == {"token": "T", "room_id": "R"}


def test_send_webex_requires_token_and_room(monkeypatch):
    import pytest
    with pytest.raises(ValueError):
        notifymod.send_webex({"token": "T"}, "t", "x")   # missing room_id
