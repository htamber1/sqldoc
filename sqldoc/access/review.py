"""Access review — scan logins + database role memberships and flag risks.

Flags, per configured server/database:

* **inactive** accounts (no activity for more than `inactive_days`);
* **over_privileged** accounts (more access than their AD job title suggests);
* **sod** — separation-of-duties violations (a principal that can both modify
  data *and* grant/approve access);
* **orphaned** Windows logins with no backing AD object;
* **service_account** accounts holding excessive (admin) permissions.

Each finding carries a generated fix script. Best-effort + isolated per
server/database.
"""
from datetime import datetime, timezone

from sqldoc.dbutil import cell
from sqldoc.access.model import ReviewFinding
from sqldoc.access.roles import roles_for_level
from sqldoc.access.script import _q
from sqldoc.access.sqlserver import (
    collect_server_logins, ROLE_LEVEL, _name_part,
    DB_PRINCIPALS_SQL, DB_ROLE_MEMBERS_SQL, DB_PERMISSIONS_SQL)
from sqldoc.access.titles import expected_level_for_title, exceeds, is_service_account

LOGIN_ACTIVITY_SQL = """
    /* ACCESS_LOGIN_ACTIVITY */
    SELECT login_name AS name, MAX(last_request_end_time) AS last_activity
    FROM sys.dm_exec_sessions
    WHERE login_name IS NOT NULL
    GROUP BY login_name
"""

_WRITE_ROLES = {"db_datawriter", "db_owner", "db_ddladmin", "db_backupoperator"}
_APPROVE_ROLES = {"db_securityadmin", "db_accessadmin", "db_owner"}
_SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _max_level(roles, perms):
    from sqldoc.access.sqlserver import _perm_level, _max_level as mx
    levels = [ROLE_LEVEL.get(r, "none") for r in roles]
    levels += [_perm_level(p, st) for (p, st, _s, _o) in perms]
    return mx(levels)


def _drop_roles_sql(database, member, roles):
    lines = [f"USE {_q(database)};", "GO"]
    for r in roles:
        lines.append(f"ALTER ROLE {_q(r)} DROP MEMBER {_q(member)};")
    return "\n".join(lines) + "\nGO\n"


def login_activity(cursor) -> dict:
    """login_name -> last activity ISO string (best-effort; DMV shows only
    sessions the instance still remembers, so absence != truly inactive)."""
    try:
        cursor.execute(LOGIN_ACTIVITY_SQL)
    except Exception:
        return {}
    out = {}
    for r in cursor.fetchall():
        out[cell(r, "name")] = cell(r, "last_activity")
    return out


def _iso(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def review_logins(cursor, server, source, inactive_days, now_epoch, service_patterns=None) -> list:
    """Server-level checks: orphaned Windows logins + inactivity."""
    findings = []
    logins = collect_server_logins(cursor)
    activity = login_activity(cursor)
    cutoff = now_epoch - inactive_days * 86400

    for lg in logins:
        # Orphaned individual Windows login (its AD user no longer resolves).
        if source is not None and "WINDOWS_LOGIN" in (lg.type or "").upper():
            try:
                u = source.get_user(_name_part(lg.name))
                if not u.found:
                    findings.append(ReviewFinding(
                        category="orphaned", severity="HIGH", principal=lg.name, server=server,
                        summary=f"Login {lg.name} has no backing AD account",
                        detail="The Windows login references an AD user that no longer exists. "
                               "Orphaned logins are a security and audit liability.",
                        fix_sql=f"-- Remove the orphaned login (after confirming no dependencies)\n"
                                f"DROP LOGIN {_q(lg.name)};\nGO\n"))
                    continue
            except Exception:
                pass

        # Inactivity (only when we have a genuine old timestamp).
        last = _iso(activity.get(lg.name))
        if last:
            try:
                dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.timestamp() < cutoff:
                    days = int((now_epoch - dt.timestamp()) / 86400)
                    findings.append(ReviewFinding(
                        category="inactive", severity="MEDIUM", principal=lg.name, server=server,
                        summary=f"Login {lg.name} inactive for ~{days} days",
                        detail=f"Last observed activity was {last} (> {inactive_days} day threshold).",
                        fix_sql=f"-- Disable the stale login (reversible)\n"
                                f"ALTER LOGIN {_q(lg.name)} DISABLE;\nGO\n"))
            except (ValueError, AttributeError):
                pass
    return findings


def review_database(cursor, server, database, source, service_patterns=None) -> list:
    """Per-database checks: over-privilege, SoD, service-account excess."""
    findings = []
    cursor.execute(DB_PRINCIPALS_SQL)
    principals = {cell(r, "db_user"): cell(r, "type_desc") for r in cursor.fetchall()}

    cursor.execute(DB_ROLE_MEMBERS_SQL)
    roles_by = {}
    for r in cursor.fetchall():
        roles_by.setdefault(cell(r, "member_name"), []).append(cell(r, "role_name"))

    cursor.execute(DB_PERMISSIONS_SQL)
    perms_by = {}
    for r in cursor.fetchall():
        perms_by.setdefault(cell(r, "principal_name"), []).append((
            cell(r, "permission_name"), cell(r, "state_desc"),
            cell(r, "schema_name"), cell(r, "object_name")))

    for member, ptype in principals.items():
        roles = sorted(roles_by.get(member, []))
        perms = perms_by.get(member, [])
        if not roles and not perms:
            continue
        level = _max_level(roles, perms)
        role_set = set(roles)

        # SoD: can modify data AND grant/approve access.
        if (role_set & _WRITE_ROLES) and (role_set & _APPROVE_ROLES):
            offending = sorted((role_set & _WRITE_ROLES) | (role_set & _APPROVE_ROLES))
            findings.append(ReviewFinding(
                category="sod", severity="HIGH", principal=member, server=server, database=database,
                summary=f"{member} can both modify and approve data in {database}",
                detail=f"Holds write roles and security/approval roles ({', '.join(offending)}) — "
                       "a separation-of-duties violation; the same person can change data and grant access.",
                fix_sql=_drop_roles_sql(database, member,
                                        sorted(role_set & _APPROVE_ROLES) or ["db_securityadmin"])))

        # Service account with excessive (admin) permissions.
        if is_service_account(member, service_patterns) and level == "admin":
            findings.append(ReviewFinding(
                category="service_account", severity="HIGH", principal=member, server=server,
                database=database,
                summary=f"Service account {member} has admin rights in {database}",
                detail=f"Service accounts should run least-privilege; this one holds admin-level "
                       f"access ({', '.join(roles) or 'via grants'}).",
                fix_sql=_drop_roles_sql(database, member,
                                        [r for r in roles if ROLE_LEVEL.get(r) == "admin"] or ["db_owner"])))

        # Over-privileged vs AD title (individual accounts only).
        if source is not None and "WINDOWS_USER" in (ptype or "").upper() and "\\" in member:
            try:
                u = source.get_user(_name_part(member))
            except Exception:
                u = None
            if u is not None and u.found and u.title:
                expected = expected_level_for_title(u.title)
                if exceeds(level, expected, by=2):
                    findings.append(ReviewFinding(
                        category="over_privileged", severity="MEDIUM", principal=member,
                        server=server, database=database,
                        summary=f"{member} ({u.title}) has {level} access in {database}, "
                                f"title suggests {expected}",
                        detail=f"Access level exceeds what the job title '{u.title}' typically "
                               f"justifies. Review against least-privilege.",
                        fix_sql=_drop_roles_sql(database, member,
                                                [r for r in roles if r not in roles_for_level(expected)])))
    return findings


def review_access(cfg, source=None, adapter_factory=None, inactive_days=90,
                  now_epoch=None, service_patterns=None) -> list:
    """Run the full review across configured servers/databases. Returns findings
    sorted most-severe first."""
    from sqldoc.access import ad as ad_mod
    from sqldoc.access import config as access_config
    from sqldoc.access.checker import build_db_adapter

    if now_epoch is None:
        now_epoch = datetime.now(timezone.utc).timestamp()
    if source is None and access_config.ad_config(cfg):
        try:
            source = ad_mod.get_source(access_config.ad_config(cfg))
        except Exception:
            source = None
    factory = adapter_factory or build_db_adapter

    findings = []
    for entry in access_config.servers(cfg):
        server_name = entry["name"]
        server_checked = False
        for database in entry["databases"]:
            try:
                adapter = factory(entry, database)
                conn = adapter.connect()
                try:
                    cursor = adapter.cursor(conn)
                    if not server_checked:
                        findings += review_logins(cursor, server_name, source,
                                                  inactive_days, now_epoch, service_patterns)
                        server_checked = True
                    findings += review_database(cursor, server_name, database, source,
                                                service_patterns)
                finally:
                    conn.close()
            except Exception as e:
                findings.append(ReviewFinding(
                    category="error", severity="LOW", principal="",
                    server=server_name, database=database,
                    summary=f"Review skipped for {server_name}/{database}",
                    detail=f"{type(e).__name__}: {e}"))
    findings.sort(key=lambda f: (_SEVERITY_RANK.get(f.severity, 3), f.category, f.principal))
    return findings
