"""Access approval workflow: approver routing, submit/email, decide, audit, Jira."""
import pytest

from sqldoc.access import approval
from sqldoc.access.approval import approver_for, submit_approval, record_decision, get_approval, pending
from sqldoc.access.model import GeneratedScript


CFG = {"access": {
    "approvers": {"Sales.HR": "hr-lead@corp.com", "Sales": "sales-dba@corp.com",
                  "default": "dba@corp.com"},
    "email": {"smtp_host": "smtp.corp", "to": ["dba@corp.com"]},
    "approval_base_url": "https://sqldoc.corp",
}}


def _script(database="Sales"):
    return GeneratedScript(server="prod", database=database, login_name="CORP\\Sales Team",
                           role="db_datareader, db_datawriter",
                           grant_sql="ALTER ROLE [db_datawriter] ADD MEMBER [CORP\\Sales Team];",
                           rollback_sql="ALTER ROLE [db_datawriter] DROP MEMBER [CORP\\Sales Team];",
                           uses_windows_group=True)


# --- approver routing ------------------------------------------------------

def test_approver_for_specificity():
    assert approver_for(CFG, "Sales", "HR") == "hr-lead@corp.com"
    assert approver_for(CFG, "Sales", None) == "sales-dba@corp.com"
    assert approver_for(CFG, "Other", None) == "dba@corp.com"       # default


def test_approver_for_no_config():
    assert approver_for({"access": {}}, "Sales") is None


# --- submit ----------------------------------------------------------------

def test_submit_sends_email():
    sent = []
    rec = submit_approval(CFG, _script(), requester="jsmith", ticket="SEC-1", schema="HR",
                          mailer=lambda smtp, subj, html: sent.append((smtp, subj, html)))
    assert rec["status"] == "pending" and rec["approver"] == "hr-lead@corp.com"
    assert rec["sent"] is True and sent
    # email contains the approve/reject links + the script
    assert rec["token"] in sent[0][2] and "APPROVE" in sent[0][2]
    assert "ALTER ROLE" in sent[0][2]
    # persisted + retrievable
    assert get_approval(rec["token"])["ticket"] == "SEC-1"
    assert any(r["token"] == rec["token"] for r in pending())


def test_submit_no_approver_still_records():
    rec = submit_approval({"access": {}}, _script(), requester="jsmith",
                          mailer=lambda *a: None)
    assert rec["approver"] == "" and rec["sent"] is False
    assert get_approval(rec["token"]) is not None


def test_submit_email_failure_is_captured():
    def boom(smtp, subj, html):
        raise RuntimeError("smtp down")
    rec = submit_approval(CFG, _script(), requester="jsmith", mailer=boom)
    assert rec["sent"] is False and "smtp down" in rec["send_error"]


# --- decide ----------------------------------------------------------------

def test_approve_logs_to_audit(monkeypatch):
    logged = []
    import sqldoc.audit as audit_mod
    monkeypatch.setattr(audit_mod, "record",
                        lambda **k: logged.append(k) or type("E", (), k)())
    rec = submit_approval(CFG, _script(), requester="jsmith", mailer=lambda *a: None)
    out = record_decision(CFG, rec["token"], "approve")
    assert out["status"] == "approved"
    assert logged and logged[0]["command"] == "access.approve" and logged[0]["result"] == "approved"


def test_reject_posts_jira_comment():
    class FakeJira:
        def __init__(self):
            self.commented = None

        def add_comment(self, key, adf):
            self.commented = (key, adf)
    fake = FakeJira()
    rec = submit_approval(CFG, _script(), requester="jsmith", ticket="SEC-9", mailer=lambda *a: None)
    out = record_decision(CFG, rec["token"], "reject", reason="too broad", jira_client=fake)
    assert out["status"] == "rejected" and out["reason"] == "too broad"
    assert fake.commented and fake.commented[0] == "SEC-9"


def test_decide_unknown_token():
    with pytest.raises(ValueError):
        record_decision(CFG, "no-such-token", "approve")


def test_decide_twice_is_idempotent():
    rec = submit_approval(CFG, _script(), requester="jsmith", mailer=lambda *a: None)
    record_decision(CFG, rec["token"], "approve")
    again = record_decision(CFG, rec["token"], "reject")
    assert again["status"] == "approved" and "already" in again.get("note", "")


# --- CLI -------------------------------------------------------------------

def test_cli_approve_submit_and_decide(monkeypatch, tmp_path):
    import yaml
    from click.testing import CliRunner
    from sqldoc import cli
    from sqldoc.access import checker
    from sqldoc.access.model import AccessReport, ADUser, Login
    r = AccessReport(user=ADUser(identifier="jsmith", login="CORP\\jsmith", groups=["Sales Team"], found=True))
    r.logins.append(Login(name="CORP\\Sales Team", type="WINDOWS_GROUP"))
    monkeypatch.setattr(checker, "check_access", lambda c, ident, **k: r)
    monkeypatch.setattr(cli, "_access_tables_for", lambda cfg, db: ([], [], "prod", None))
    monkeypatch.setattr(approval, "_default_mailer", lambda smtp, subj, html: None)
    cfg = {"access": {**CFG["access"],
                      "ad": {"type": "ldap", "server": "x", "base_dn": "y"},
                      "servers": [{"name": "prod", "connection_string": "c",
                                   "dialect": "sqlserver", "databases": ["Sales"]}]}}
    p = tmp_path / ".sqldoc.yml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    res = CliRunner().invoke(cli.cli, ["access", "approve", "--config", str(p), "--user", "jsmith",
                                       "--database", "Sales", "--level", "write", "--requester", "jsmith"])
    assert res.exit_code == 0, res.output
    assert "token:" in res.output
    token = [l for l in res.output.splitlines() if "token:" in l][0].split()[-1]

    res2 = CliRunner().invoke(cli.cli, ["access", "approve", "--config", str(p),
                                        "--token", token, "--decision", "approve"])
    assert res2.exit_code == 0, res2.output
    assert "approved" in res2.output
    assert get_approval(token)["status"] == "approved"
