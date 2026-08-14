"""SQL Server catalog probes + AD-to-SQL cross-reference for the access suite.

All functions take a live DB cursor (so the checker controls one connection per
database) and read only catalog views — no row data. The cross-reference maps a
resolved :class:`~sqldoc.access.model.ADUser` to the server logins that grant
them access (their own Windows login + every Windows-group login for a group they
belong to), then to the database roles/permissions those logins carry.
"""
from sqldoc.dbutil import cell
from sqldoc.access.model import DatabaseAccess, Login, LEVEL_ORDER

# Fixed database roles -> the coarse level they confer (deny roles confer none).
ROLE_LEVEL = {
    "db_owner": "admin", "db_securityadmin": "admin", "db_accessadmin": "admin",
    "db_ddladmin": "admin",
    "db_datawriter": "write", "db_backupoperator": "write",
    "db_datareader": "read",
    "db_denydatareader": "none", "db_denydatawriter": "none",
}
_READ_ALL_ROLES = {"db_datareader", "db_owner"}

# Fixed SERVER roles that confer access to every database implicitly, with no
# database principal required. A sysadmin bypasses all permission checks and maps
# to dbo in each database, so a login holding it has full access even though
# sys.database_principals shows nothing for it. securityadmin is included because
# it can grant itself any permission, so it is effectively unbounded.
SERVER_ROLE_LEVEL = {"sysadmin": "admin", "securityadmin": "admin"}

# Server-scoped PERMISSIONS (not roles, so they are absent from
# sys.server_role_members) that are equivalent to sysadmin in reach.
# ALTER ANY LOGIN is included because its holder can reset any SQL login's
# password (including sa) and so can reach sysadmin at will.
SERVER_PERMISSION_LEVEL = {"CONTROL SERVER": "admin", "ALTER ANY LOGIN": "admin"}

# IMPERSONATE is only an escalation when its TARGET is privileged; collection
# records it in this resolved form, e.g. "IMPERSONATE ON LOGIN::sa".
IMPERSONATE_PREFIX = "IMPERSONATE ON "

# Permissions that are an escalation *route* rather than access in themselves:
# the holder can take control, but has not been given it. Reported as a flag so
# a reviewer sees the risk without the effective level being overstated.
ESCALATION_FLAG_PERMISSIONS = {
    "TAKE OWNERSHIP": "can seize ownership of the securable (escalation route)",
}


def _permission_level(perm: str) -> str:
    """The coarse level a single server-scoped permission confers."""
    p = (perm or "").strip().upper()
    if p.startswith(IMPERSONATE_PREFIX):
        return "admin"          # only privileged targets are recorded this way
    return SERVER_PERMISSION_LEVEL.get(p, "none")


def _server_implied_level(login) -> str:
    """The coarse level `login` holds server-wide, from fixed server roles and
    server-scoped permissions, independent of any database principal."""
    levels = [SERVER_ROLE_LEVEL.get((r or "").lower(), "none")
              for r in (getattr(login, "server_roles", None) or [])]
    levels += [_permission_level(p)
               for p in (getattr(login, "server_permissions", None) or [])]
    return _max_level(levels)


def _server_grantors(login) -> list:
    """The specific roles/permissions responsible for a server-wide level."""
    return ([r for r in (getattr(login, "server_roles", None) or [])
             if (r or "").lower() in SERVER_ROLE_LEVEL]
            + [p for p in (getattr(login, "server_permissions", None) or [])
               if _permission_level(p) != "none"])


def _escalation_flags(perms) -> list:
    """Escalation-route notes for permissions that are flagged, not levelled."""
    out = []
    for p, st in perms or []:
        if (st or "").upper().startswith("DENY"):
            continue
        note = ESCALATION_FLAG_PERMISSIONS.get((p or "").strip().upper())
        if note and note not in out:
            out.append(f"{(p or '').strip().upper()}: {note}")
    return out

SERVER_LOGINS_SQL = """
    /* ACCESS_SERVER_LOGINS */
    SELECT sp.name AS name, sp.type_desc AS type_desc, sp.is_disabled AS is_disabled
    FROM sys.server_principals sp
    WHERE sp.type IN ('U', 'G', 'S')
    ORDER BY sp.name
"""

SERVER_ROLE_MEMBERS_SQL = """
    /* ACCESS_SERVER_ROLE_MEMBERS */
    SELECT r.name AS role_name, m.name AS member_name
    FROM sys.server_role_members srm
    INNER JOIN sys.server_principals r ON srm.role_principal_id = r.principal_id
    INNER JOIN sys.server_principals m ON srm.member_principal_id = m.principal_id
    ORDER BY r.name, m.name
"""

SERVER_PERMISSIONS_SQL = """
    /* ACCESS_SERVER_PERMISSIONS */
    SELECT pr.name AS principal_name, perm.permission_name AS permission_name,
           perm.state_desc AS state_desc, perm.class AS perm_class,
           tgt.name AS target_name
    FROM sys.server_permissions perm
    INNER JOIN sys.server_principals pr ON perm.grantee_principal_id = pr.principal_id
    LEFT JOIN sys.server_principals tgt
           ON perm.class = 101 AND perm.major_id = tgt.principal_id
    WHERE perm.class IN (100, 101)
"""

DB_PRINCIPALS_SQL = """
    /* ACCESS_DB_PRINCIPALS */
    SELECT dp.name AS db_user, dp.type_desc AS type_desc
    FROM sys.database_principals dp
    WHERE dp.type IN ('U', 'G', 'S')
"""

DB_ROLE_MEMBERS_SQL = """
    /* ACCESS_DB_ROLE_MEMBERS */
    SELECT r.name AS role_name, m.name AS member_name
    FROM sys.database_role_members rm
    INNER JOIN sys.database_principals r ON rm.role_principal_id = r.principal_id
    INNER JOIN sys.database_principals m ON rm.member_principal_id = m.principal_id
"""

DB_PERMISSIONS_SQL = """
    /* ACCESS_DB_PERMISSIONS */
    SELECT pr.name AS principal_name, perm.permission_name AS permission_name,
           perm.state_desc AS state_desc, s.name AS schema_name, o.name AS object_name
    FROM sys.database_permissions perm
    INNER JOIN sys.database_principals pr ON perm.grantee_principal_id = pr.principal_id
    INNER JOIN sys.objects o ON perm.major_id = o.object_id
    INNER JOIN sys.schemas s ON o.schema_id = s.schema_id
    WHERE perm.class = 1 AND o.type IN ('U', 'V')
"""

# Database-SCOPED grants (class 0), e.g. GRANT CONTROL ON DATABASE::X. These have
# no major_id object, so the object-level query above (class 1, joined to
# sys.objects) can never return them -- yet CONTROL on the database confers full
# control of everything in it.
DB_SCOPED_PERMISSIONS_SQL = """
    /* ACCESS_DB_SCOPED_PERMISSIONS */
    SELECT pr.name AS principal_name, perm.permission_name AS permission_name,
           perm.state_desc AS state_desc, perm.class AS perm_class,
           tgt.name AS target_name
    FROM sys.database_permissions perm
    INNER JOIN sys.database_principals pr ON perm.grantee_principal_id = pr.principal_id
    LEFT JOIN sys.database_principals tgt
           ON perm.class = 4 AND perm.major_id = tgt.principal_id
    WHERE perm.class IN (0, 4)
"""


def _opt(row, name, default=None):
    """Read an optional column, tolerating a row shape that omits it.

    `perm_class` / `target_name` enrich the permission queries; a row without
    them should degrade to the un-enriched behaviour, never crash the audit.
    """
    try:
        return cell(row, name)
    except Exception:
        return default


def _fetch(cursor, sql) -> list:
    """Run a supplementary query, tolerating a permission error.

    These widen what sqldoc can see; if the caller lacks rights to read them the
    audit should degrade to the previous behaviour rather than fail outright.
    """
    try:
        cursor.execute(sql)
        return list(cursor.fetchall())
    except Exception:
        return []


def _name_part(name: str) -> str:
    """The account/group part of a login, stripping any DOMAIN\\ prefix."""
    return (name or "").split("\\")[-1]


def collect_server_logins(cursor) -> list:
    cursor.execute(SERVER_LOGINS_SQL)
    logins = [
        Login(name=cell(r, "name"), type=cell(r, "type_desc"),
              is_disabled=bool(int(cell(r, "is_disabled") or 0)))
        for r in cursor.fetchall()
    ]
    cursor.execute(SERVER_ROLE_MEMBERS_SQL)
    roles_by_member = {}
    for r in cursor.fetchall():
        roles_by_member.setdefault(cell(r, "member_name"), []).append(cell(r, "role_name"))
    # Server-scoped permissions (CONTROL SERVER, ALTER ANY LOGIN) are grants, not
    # role memberships, so they never appear in sys.server_role_members.
    rows = _fetch(cursor, SERVER_PERMISSIONS_SQL)
    # Logins that are privileged targets: impersonating one of these is an
    # escalation, impersonating an ordinary login is not.
    privileged = {m for m, rs in roles_by_member.items()
                  if any((r or "").lower() in SERVER_ROLE_LEVEL for r in rs)}
    privileged |= {cell(r, "principal_name") for r in rows
                   if (cell(r, "permission_name") or "").strip().upper()
                   in SERVER_PERMISSION_LEVEL
                   and not (cell(r, "state_desc") or "").upper().startswith("DENY")}
    privileged.add("sa")

    perms_by_member = {}
    for r in rows:
        if (cell(r, "state_desc") or "").upper().startswith("DENY"):
            continue
        name = (cell(r, "permission_name") or "").strip().upper()
        target = _opt(r, "target_name")
        if name == "IMPERSONATE":
            # Only record it when the impersonated login is itself privileged.
            if not target or target not in privileged:
                continue
            name = f"{IMPERSONATE_PREFIX}LOGIN::{target}"
        perms_by_member.setdefault(cell(r, "principal_name"), []).append(name)
    for lg in logins:
        lg.server_roles = sorted(roles_by_member.get(lg.name, []))
        lg.server_permissions = sorted(set(perms_by_member.get(lg.name, [])))
    return logins


def match_user_logins(logins, user) -> list:
    """The logins that grant this user access: their own Windows login and every
    Windows-group login for a group they belong to. Name-part matched
    case-insensitively so it's domain-naming agnostic."""
    group_parts = {_name_part(g).lower() for g in (user.groups or [])}
    group_parts |= {g.lower() for g in (user.groups or [])}   # also bare CNs
    sam = (user.sam_account_name or "").lower()
    login_full = (user.login or "").lower()
    matched = []
    for lg in logins:
        part = _name_part(lg.name).lower()
        full = lg.name.lower()
        tdesc = (lg.type or "").upper()
        if "GROUP" in tdesc:
            if part in group_parts or full in group_parts:
                matched.append(lg)
        else:  # windows/sql login -> the user themselves
            if sam and (part == sam or full == login_full):
                matched.append(lg)
    return matched


def _max_level(levels) -> str:
    best = "none"
    for lv in levels:
        if LEVEL_ORDER.get(lv, 0) > LEVEL_ORDER.get(best, 0):
            best = lv
    return best


def _perm_level(permission: str, state: str) -> str:
    from sqldoc.comply import classify_permission
    if (state or "").upper().startswith("DENY"):
        return "none"
    return classify_permission(permission, state)


def collect_db_access(cursor, server, database, matched_logins, pii_findings) -> list:
    """Effective access each matched login has in this database."""
    cursor.execute(DB_PRINCIPALS_SQL)
    db_users = {cell(r, "db_user"): cell(r, "type_desc") for r in cursor.fetchall()}

    cursor.execute(DB_ROLE_MEMBERS_SQL)
    roles_by_user = {}
    for r in cursor.fetchall():
        roles_by_user.setdefault(cell(r, "member_name"), []).append(cell(r, "role_name"))

    # Database principals worth impersonating: dbo and any db_owner member.
    privileged_dbusers = {"dbo"} | {u for u, rs in roles_by_user.items()
                                    if "db_owner" in [(r or "").lower() for r in rs]}
    dbscope_by_user = {}
    for r in _fetch(cursor, DB_SCOPED_PERMISSIONS_SQL):
        name = (cell(r, "permission_name") or "").strip().upper()
        if int(_opt(r, "perm_class", 0) or 0) == 4:
            # class 4 = DATABASE_PRINCIPAL; only IMPERSONATE of a privileged
            # principal is an escalation, and it is levelled as admin.
            target = _opt(r, "target_name")
            if name != "IMPERSONATE" or not target or target not in privileged_dbusers:
                continue
            name = f"{IMPERSONATE_PREFIX}USER::{target}"
        dbscope_by_user.setdefault(cell(r, "principal_name"), []).append(
            (name, cell(r, "state_desc")))

    cursor.execute(DB_PERMISSIONS_SQL)
    perms_by_user = {}
    for r in cursor.fetchall():
        perms_by_user.setdefault(cell(r, "principal_name"), []).append((
            cell(r, "permission_name"), cell(r, "state_desc"),
            cell(r, "schema_name"), cell(r, "object_name")))

    # Index PII findings by (schema, table).
    pii_by_table = {}
    for f in pii_findings or []:
        key = (f.schema, f.table)
        cur = pii_by_table.setdefault(key, {"risk": f.risk, "regs": set()})
        cur["regs"].update(f.regulations or [])

    out = []
    # Match db users case-insensitively against the login name (Windows group /
    # user database users typically share the login name).
    lc_users = {u.lower(): u for u in db_users}
    for lg in matched_logins:
        db_user = lc_users.get(lg.name.lower()) or lc_users.get(_name_part(lg.name).lower())
        # A sysadmin (or CONTROL SERVER holder) needs no database principal — it
        # reaches every database regardless — so a missing db_user must not drop
        # the login here.
        implied = _server_implied_level(lg)
        if not db_user and implied == "none":
            continue
        roles = sorted(roles_by_user.get(db_user, [])) if db_user else []
        perms = perms_by_user.get(db_user, []) if db_user else []
        # Database-scoped grants (CONTROL ON DATABASE) apply to everything in the
        # database and carry no object, so they are levelled separately.
        scoped = dbscope_by_user.get(db_user, []) if db_user else []
        scoped_levels = []
        for (p, st) in scoped:
            pu = (p or "").strip().upper()
            if pu in ESCALATION_FLAG_PERMISSIONS:
                continue                      # flagged below, never levelled
            if pu.startswith(IMPERSONATE_PREFIX):
                scoped_levels.append(
                    "none" if (st or "").upper().startswith("DENY") else "admin")
            else:
                scoped_levels.append(_perm_level(p, st))
        reads_whole_db = any(lv in ("read", "admin") for lv in scoped_levels)
        # Escalation routes are reported, never folded into the effective level.
        flags = _escalation_flags(scoped) + _escalation_flags(
            [(p, st) for (p, st, _s, _o) in perms])
        levels = [ROLE_LEVEL.get(r, "none") for r in roles]
        levels += [_perm_level(p, st) for (p, st, _s, _o) in perms]
        levels += scoped_levels
        levels.append(implied)
        level = _max_level(levels)

        # PII tables the user can read: everything if a read-all role, else the
        # specific tables they hold a non-deny grant on.
        pii_tables = []
        if (set(roles) & _READ_ALL_ROLES) or implied == "admin" or reads_whole_db:
            for (schema, table), info in pii_by_table.items():
                pii_tables.append((schema, table, info["risk"], sorted(info["regs"])))
        else:
            for (p, st, sch, obj) in perms:
                if (st or "").upper().startswith("DENY"):
                    continue
                info = pii_by_table.get((sch, obj))
                if info:
                    pii_tables.append((sch, obj, info["risk"], sorted(info["regs"])))
        pii_tables = sorted(set((s, t, r, tuple(g)) for (s, t, r, g) in pii_tables))
        pii_tables = [(s, t, r, list(g)) for (s, t, r, g) in pii_tables]

        via = ("group " + lg.name) if "GROUP" in (lg.type or "").upper() else "direct login"
        if implied != "none":
            granting = ", ".join(_server_grantors(lg))
            if granting:
                via += f" -> server-wide {granting}"
        out.append(DatabaseAccess(
            server=server, database=database, login=lg.name, db_user=db_user or "",
            via=via, roles=roles, permissions=perms, level=level,
            pii_tables=pii_tables, flags=sorted(set(flags))))
    return out
