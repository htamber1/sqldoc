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
    collect_server_logins, collect_db_principals, ROLE_LEVEL, _name_part,
    AUTH_INSTANCE, AUTH_WINDOWS, WINDOWS_PRINCIPAL_TYPES,
    DB_PRINCIPALS_SQL, DB_ROLE_MEMBERS_SQL, DB_PERMISSIONS_SQL)
from sqldoc.access.titles import expected_level_for_title, exceeds, is_service_account

# Windows authorities whose principals are built-in or virtual: they are created
# and managed by the OS / SQL Server setup and never appear in Active Directory.
BUILTIN_LOGIN_AUTHORITIES = {
    "NT AUTHORITY",          # SYSTEM, LOCAL SERVICE, NETWORK SERVICE, ...
    "NT SERVICE",            # MSSQLSERVER, SQLSERVERAGENT, SQLWriter, Winmgmt, ...
    "BUILTIN",               # Administrators, Users
    "NT VIRTUAL MACHINE",
    "IIS APPPOOL",
    "APPLICATION PACKAGE AUTHORITY",
}


def is_builtin_principal(name: str) -> bool:
    """True for a Windows built-in/virtual login (``NT SERVICE\\MSSQLSERVER``)."""
    n = (name or "").strip()
    if "\\" not in n:
        return False
    return n.split("\\", 1)[0].strip().upper() in BUILTIN_LOGIN_AUTHORITIES


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
        # Windows built-in / virtual accounts are platform-managed and have no AD
        # object by definition, so the orphan check below would flag every one of
        # them — and its fix script would DROP LOGIN the account SQL Server
        # itself runs under. They are not identities an access review governs.
        if is_builtin_principal(lg.name):
            continue
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


def review_db_principals(cursor, server, database, logins=None,
                         include_read_principals=False) -> list:
    """Database principals whose access does not trace back to a server login.

    Three genuinely different situations, deliberately reported as three
    categories because they have three different fixes:

    * **db_principal_no_login** — a Windows user or group granted directly in the
      database. SQL Server authorises it from the connecting session's Windows
      token, so this is LIVE, usable access that a login-first audit cannot see.
    * **orphaned_db_user** — a SQL user whose login is gone (SID no longer
      resolves). Broken rather than dangerous: nobody can authenticate as it.
    * ``AUTHENTICATION_TYPE = NONE`` — a deliberate ``CREATE USER ... WITHOUT
      LOGIN`` used for module signing and ``EXECUTE AS``. **Never** reported.

    Severity for the live class tracks the effective level, and read-level
    principals are suppressed unless `include_read_principals` is set. Estates
    that standardise on database-only role groups have thousands of them (~1,180
    were measured across four dev servers); emitting every one at a fixed
    severity would bury the handful that matter. An unnoticed db_owner with no
    login is a real finding; a read-only role group is the intended design.
    """
    principals = collect_db_principals(cursor)
    if not principals:
        return []                      # linkage unreadable -> report nothing
    findings = []
    login_names = {(l.name or "").lower() for l in (logins or [])}

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

    for name, meta in sorted(principals.items()):
        if meta.get("has_login") or is_builtin_principal(name):
            continue
        auth, ptype = meta.get("auth"), meta.get("type")
        roles = sorted(roles_by.get(name, []))
        perms = perms_by.get(name, [])
        if not roles and not perms:
            continue               # no entitlements: nothing to report either way
        level = _max_level(roles, perms)
        held = ", ".join(roles) or "explicit grants"

        if ptype in WINDOWS_PRINCIPAL_TYPES and auth == AUTH_WINDOWS:
            severity = {"admin": "HIGH", "write": "MEDIUM"}.get(level, "LOW")
            if severity == "LOW" and not include_read_principals:
                continue
            kind = "group" if ptype == "WINDOWS_GROUP" else "user"
            findings.append(ReviewFinding(
                category="db_principal_no_login", severity=severity,
                principal=name, server=server, database=database,
                summary=f"{name} has {level} access in {database} with no server login",
                detail=f"This Windows {kind} is a database principal in {database} "
                       f"holding {held}, but has no login on the instance. SQL Server "
                       f"authorises it by SID from the connecting session's Windows "
                       f"token, so the access is real and usable — yet it is invisible "
                       f"to any audit that enumerates server logins first. Confirm it "
                       f"is intentional; if it is, it still belongs in the access "
                       f"inventory.",
                # Deliberately advisory: dropping a live group principal revokes
                # real access from every member of it.
                fix_sql=f"-- REVIEW ONLY — do not run blind. This principal grants real\n"
                        f"-- access to everyone in it; dropping it revokes that access.\n"
                        f"-- Intentional? record it in the access inventory.\n"
                        f"-- Not intentional? then, after confirming no dependencies:\n"
                        f"-- USE {_q(database)};\n-- DROP USER {_q(name)};\n"))
        elif auth == AUTH_INSTANCE and name.lower() not in login_names:
            # SQL user whose login no longer exists, or whose SID drifted after a
            # restore. Not a live risk -- it cannot be authenticated as -- but it
            # is dead entitlement and a standard audit finding.
            findings.append(ReviewFinding(
                category="orphaned_db_user", severity="MEDIUM",
                principal=name, server=server, database=database,
                summary=f"Database user {name} in {database} has no matching login",
                detail=f"The user holds {held} but its SID matches no server login, so "
                       f"nobody can authenticate as it (typically a restored database). "
                       f"The entitlement is dead but still granted.",
                fix_sql=f"-- Remap to an existing login of the same name, if there is one:\n"
                        f"USE {_q(database)};\n"
                        f"ALTER USER {_q(name)} WITH LOGIN = {_q(name)};\nGO\n"
                        f"-- If no such login should exist, drop the user instead:\n"
                        f"-- DROP USER {_q(name)};\n"))
        # auth == NONE (CREATE USER ... WITHOUT LOGIN) falls through: deliberate.
    return findings


def review_access(cfg, source=None, adapter_factory=None, inactive_days=90,
                  now_epoch=None, service_patterns=None,
                  include_read_principals=False) -> list:
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
        server_logins = []
        for database in entry["databases"]:
            try:
                adapter = factory(entry, database)
                conn = adapter.connect()
                try:
                    cursor = adapter.cursor(conn)
                    if not server_checked:
                        findings += review_logins(cursor, server_name, source,
                                                  inactive_days, now_epoch, service_patterns)
                        # Read once per server and reuse: the database-principal
                        # check needs the login list to tell a drifted SID from a
                        # login that simply is not there.
                        server_logins = collect_server_logins(cursor)
                        server_checked = True
                    findings += review_database(cursor, server_name, database, source,
                                                service_patterns)
                    findings += review_db_principals(
                        cursor, server_name, database, logins=server_logins,
                        include_read_principals=include_read_principals)
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
