"""Email-based approval workflow for generated access scripts.

A generated script can be submitted for approval: the designated approver (per
database or schema, configurable) is emailed the script with approve/reject
links. Approved scripts are logged to the audit trail; rejected scripts post a
Jira comment explaining the rejection. Approval records live in a small JSON
store under the agent home (isolated per test via SQLDOC_AGENT_HOME).

Approver resolution (access.approvers), most specific first:
    "Sales.HR": alice@corp.com     # database.schema
    "Sales":    bob@corp.com       # database
    "default":  dba@corp.com
"""
import json
import os
import secrets
from datetime import datetime, timezone

from sqldoc.access import config as access_config


def _home() -> str:
    return os.environ.get("SQLDOC_AGENT_HOME") or os.path.join(os.path.expanduser("~"), ".sqldoc")


def _path() -> str:
    return os.path.join(_home(), "approvals.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    os.makedirs(_home(), exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def approver_for(cfg, database, schema=None):
    """Resolve the approver email for a database/schema (most specific wins)."""
    approvers = access_config.approvers(cfg)
    if schema and f"{database}.{schema}" in approvers:
        return approvers[f"{database}.{schema}"]
    if database and database in approvers:
        return approvers[database]
    return approvers.get("default")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _email_html(record, cfg) -> str:
    base = access_config.section(cfg).get("approval_base_url")
    links = ""
    if base:
        b = base.rstrip("/")
        links = (f"<p><a href='{b}/access/approve?token={record['token']}'>APPROVE</a> &nbsp; | &nbsp; "
                 f"<a href='{b}/access/reject?token={record['token']}'>REJECT</a></p>")
    cli = (f"<p>Or from the CLI:<br>"
           f"<code>sqldoc access approve --token {record['token']} --decision approve</code><br>"
           f"<code>sqldoc access approve --token {record['token']} --decision reject --reason \"...\"</code></p>")
    import html as _h
    return (
        f"<h2>Access grant approval requested</h2>"
        f"<p><b>Requester:</b> {_h.escape(record.get('requester',''))}<br>"
        f"<b>Server / database:</b> {_h.escape(record['server'])} / {_h.escape(record['database'])}<br>"
        f"<b>Grantee:</b> {_h.escape(record['login'])}<br>"
        f"<b>Role(s):</b> {_h.escape(record.get('role',''))}"
        + (f"<br><b>Jira:</b> {_h.escape(record['ticket'])}" if record.get('ticket') else "")
        + "</p>" + links + cli +
        f"<h3>Grant script</h3><pre>{_h.escape(record['grant_sql'])}</pre>"
        f"<h3>Rollback script</h3><pre>{_h.escape(record['rollback_sql'])}</pre>"
        "<p>Sent by sqldoc.</p>")


def submit_approval(cfg, script, requester, ticket=None, schema=None,
                    send_email=True, mailer=None) -> dict:
    """Create a pending approval for a generated script and email the approver."""
    approver = approver_for(cfg, script.database, schema)
    token = secrets.token_urlsafe(12)
    record = {
        "token": token, "created_at": _now(), "server": script.server,
        "database": script.database, "login": script.login_name, "role": script.role,
        "requester": requester or "", "approver": approver or "", "ticket": ticket or "",
        "grant_sql": script.grant_sql, "rollback_sql": script.rollback_sql,
        "status": "pending", "decided_at": None, "reason": None, "sent": False,
    }
    data = _load()
    data[token] = record
    _save(data)

    if send_email and approver:
        smtp = access_config.section(cfg).get("email")
        if smtp:
            try:
                send = mailer or _default_mailer
                send(smtp, f"[sqldoc] Access approval for {script.database} ({script.login_name})",
                     _email_html(record, cfg))
                record["sent"] = True
                data[token] = record
                _save(data)
            except Exception as e:
                record["send_error"] = f"{type(e).__name__}: {e}"
    return record


def _default_mailer(smtp, subject, html):
    from sqldoc.agent.notify import send_html_email
    send_html_email(smtp, subject, html)


def get_approval(token: str):
    return _load().get(token)


def pending() -> list:
    return [r for r in _load().values() if r.get("status") == "pending"]


def record_decision(cfg, token, decision, reason=None, jira_client=None, actor=None) -> dict:
    """Record an approve/reject decision. Approved -> audit trail; rejected ->
    Jira comment (if a ticket + client are available)."""
    decision = "approved" if decision in ("approve", "approved", True) else "rejected"
    data = _load()
    record = data.get(token)
    if record is None:
        raise ValueError(f"No pending approval with token '{token}'.")
    if record["status"] != "pending":
        record["note"] = f"already {record['status']}"
        return record

    record["status"] = decision
    record["decided_at"] = _now()
    record["reason"] = reason
    record["decided_by"] = actor or record.get("approver")
    data[token] = record
    _save(data)

    if decision == "approved":
        _audit_approved(record)
    elif decision == "rejected" and record.get("ticket") and jira_client is not None:
        _comment_rejection(jira_client, record)
    return record


def _audit_approved(record):
    try:
        from sqldoc import audit as audit_mod
        audit_mod.record(
            command="access.approve",
            database=record.get("database"),
            options={"server": record.get("server"), "login": record.get("login"),
                     "role": record.get("role"), "ticket": record.get("ticket"),
                     "requester": record.get("requester"), "token": record.get("token")},
            result="approved", user=record.get("decided_by") or record.get("approver"))
    except Exception:
        pass


def _comment_rejection(jira_client, record):
    try:
        from sqldoc.integrations.jira import adf_from_blocks
        reason = record.get("reason") or "(no reason given)"
        blocks = [
            ("h", "Access request rejected"),
            ("p", f"The requested {record.get('role','')} access for "
                  f"{record.get('login')} on {record.get('server')}/{record.get('database')} "
                  f"was rejected by {record.get('decided_by') or 'the approver'}."),
            ("p", f"Reason: {reason}"),
            ("p", "Rejected via the sqldoc approval workflow."),
        ]
        jira_client.add_comment(record["ticket"], adf_from_blocks(blocks))
    except Exception:
        pass
