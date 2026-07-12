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
    for lg in logins:
        lg.server_roles = sorted(roles_by_member.get(lg.name, []))
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
        if not db_user:
            continue
        roles = sorted(roles_by_user.get(db_user, []))
        perms = perms_by_user.get(db_user, [])
        levels = [ROLE_LEVEL.get(r, "none") for r in roles]
        levels += [_perm_level(p, st) for (p, st, _s, _o) in perms]
        level = _max_level(levels)

        # PII tables the user can read: everything if a read-all role, else the
        # specific tables they hold a non-deny grant on.
        pii_tables = []
        if set(roles) & _READ_ALL_ROLES:
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

        out.append(DatabaseAccess(
            server=server, database=database, login=lg.name, db_user=db_user,
            via=("group " + lg.name) if "GROUP" in (lg.type or "").upper() else "direct login",
            roles=roles, permissions=perms, level=level, pii_tables=pii_tables))
    return out
