"""Audit trail — a tamper-evident-ish log of every sqldoc command run.

Each command invocation against a database is recorded to ``~/.sqldoc/audit.log``
(one JSON object per line) and, best-effort, to the agent store's ``audit``
table. An entry captures: timestamp, command, dialect, database, OS user, the
options used (secrets redacted), and a result summary (ok / error / exit code).

`sqldoc audit` queries and exports the trail. Recording is best-effort by
design — a logging failure must never break the actual command — and secrets
(passwords, connection strings that embed them) are redacted before they are
written.
"""
import getpass
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


# Options never written to the audit log (they may hold secrets).
_REDACT_KEYS = {"password", "connection_string", "api_key"}
# Options that are noise / not decision-relevant.
_SKIP_KEYS = {"config", "yes", "verify_offline"}


def audit_log_path() -> str:
    """Path to the JSONL audit log (honours SQLDOC_AGENT_HOME like the agent)."""
    from sqldoc.agent import path_in_home
    return path_in_home("audit.log")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class AuditEntry:
    at: str
    command: str
    dialect: str = None
    database: str = None
    user: str = None
    options: dict = None
    result: str = None


def redact_options(kwargs: dict) -> dict:
    """Return a log-safe copy of a command's options: drop secrets + noise, keep
    only set (non-default-None/False) values, and note when a redacted secret
    was supplied so the audit shows a password *was* used without storing it."""
    out = {}
    for k, v in (kwargs or {}).items():
        if k in _SKIP_KEYS:
            continue
        if k in _REDACT_KEYS:
            if v:
                out[k] = "***redacted***"
            continue
        if v is None or v is False:
            continue
        out[k] = v
    return out


def _derive_database(kwargs: dict):
    db = kwargs.get("database")
    if db:
        return db
    cs = kwargs.get("connection_string")
    if cs:
        import re
        m = re.search(r'(?:DATABASE|Initial\s+Catalog)\s*=\s*([^;]+)', cs, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def record(command, dialect=None, database=None, options=None, result=None,
           user=None, log_path=None, to_store=True) -> AuditEntry:
    """Record one audit entry to the JSONL log and (best-effort) the agent store.
    Never raises — a logging failure must not break the command being audited."""
    entry = AuditEntry(
        at=_now(), command=command, dialect=dialect, database=database,
        user=user or _safe_user(), options=options or {}, result=result)
    path = log_path or audit_log_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")
    except Exception:
        # Logging must never break the command being audited.
        pass
    if to_store:
        try:
            from sqldoc.agent.store import AgentStore
            from sqldoc.agent import db_path
            AgentStore(db_path()).add_audit(
                entry.at, command, dialect, database, entry.user,
                entry.options, result)
        except Exception:
            pass
    return entry


def record_command(command, kwargs, result=None, log_path=None, to_store=True):
    """Convenience wrapper: derive dialect/database/options from a Click
    command's kwargs and record. Used by the CLI audit hook."""
    return record(
        command=command,
        dialect=kwargs.get("dialect"),
        database=_derive_database(kwargs),
        options=redact_options(kwargs),
        result=result,
        log_path=log_path, to_store=to_store)


def _safe_user():
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


# --- reading / querying / export -------------------------------------------

def read_entries(log_path=None) -> list:
    """Read all entries from the JSONL audit log (oldest first). Missing file
    yields an empty list; malformed lines are skipped."""
    path = log_path or audit_log_path()
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def query(entries, command=None, database=None, user=None, since=None) -> list:
    """Filter entries by command / database / user / since (ISO timestamp)."""
    out = entries
    if command:
        out = [e for e in out if e.get("command") == command]
    if database:
        out = [e for e in out if e.get("database") == database]
    if user:
        out = [e for e in out if e.get("user") == user]
    if since:
        out = [e for e in out if (e.get("at") or "") >= since]
    return out


def to_csv(entries) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at", "command", "dialect", "database", "user", "result", "options"])
    for e in entries:
        opts = e.get("options")
        w.writerow([e.get("at"), e.get("command"), e.get("dialect"),
                    e.get("database"), e.get("user"), e.get("result"),
                    json.dumps(opts) if opts else ""])
    return buf.getvalue()


def summarize(entries) -> dict:
    by_cmd, by_db, by_user = {}, {}, {}
    errors = 0
    for e in entries:
        by_cmd[e.get("command")] = by_cmd.get(e.get("command"), 0) + 1
        if e.get("database"):
            by_db[e["database"]] = by_db.get(e["database"], 0) + 1
        if e.get("user"):
            by_user[e["user"]] = by_user.get(e["user"], 0) + 1
        if str(e.get("result", "")).startswith("error"):
            errors += 1
    return {"total": len(entries), "errors": errors,
            "by_command": dict(sorted(by_cmd.items(), key=lambda kv: -kv[1])),
            "by_database": dict(sorted(by_db.items(), key=lambda kv: -kv[1])),
            "by_user": dict(sorted(by_user.items(), key=lambda kv: -kv[1]))}
