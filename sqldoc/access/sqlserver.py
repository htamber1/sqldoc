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

# Database principals enriched with the server-login linkage, which the plain
# query above cannot express. Kept as a SEPARATE query, fetched through _fetch,
# so an engine or permission level that cannot serve it degrades to the previous
# login-only behaviour instead of failing the whole audit.
#
# Linkage is by SID, never by name. Name matching is wrong three ways:
#   * `authentication_type_desc` does not discriminate -- a Windows principal
#     reports WINDOWS whether or not a login exists;
#   * the catalog genuinely disagrees on case (db user `DOMAIN\SVC_X` against a
#     login recorded as `DOMAIN\svc_x`), so name matching works only by
#     collation luck;
#   * it cannot see the classic restore orphan, where the name still matches but
#     the SID no longer does.
DB_PRINCIPALS_DETAIL_SQL = """
    /* ACCESS_DB_PRINCIPALS_DETAIL */
    SELECT dp.name AS db_user, dp.type_desc AS type_desc,
           dp.authentication_type_desc AS auth_type,
           CASE WHEN EXISTS (SELECT 1 FROM sys.server_principals sp
                             WHERE sp.sid = dp.sid)
                THEN 1 ELSE 0 END AS has_server_login
    FROM sys.database_principals dp
    WHERE dp.type IN ('U', 'G', 'S')
      AND dp.principal_id > 4
      AND dp.sid IS NOT NULL
"""

# Database principal types that are Windows identities. A login-less principal
# of one of these types is LIVE access; a login-less SQL_USER is a broken orphan
# and is a different finding with a different fix.
WINDOWS_PRINCIPAL_TYPES = {"WINDOWS_GROUP", "WINDOWS_USER"}

# `authentication_type_desc` values:
#   WINDOWS  -- authenticated by Windows, mapped by SID (login optional)
#   INSTANCE -- mapped to a server login by SID (missing login => orphan)
#   NONE     -- a user WITHOUT LOGIN: deliberate, used for module signing and
#               EXECUTE AS. Never a finding, and never an access path for a
#               person, so it is excluded everywhere below.
AUTH_WINDOWS = "WINDOWS"
AUTH_INSTANCE = "INSTANCE"
AUTH_NONE = "NONE"

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
    """The logins that grant this identifier access: the principal's own login
    and every Windows-group login for a group they belong to. Name-part matched
    case-insensitively so it's domain-naming agnostic.

    The identifier may itself BE a Windows group. Estates that standardise on
    role groups (grant to `DOMAIN\\GRP-App_QA`, never to a person) ask about the
    group by name, and that group is a server login in its own right. Matching
    groups only against `user.groups` meant such an identifier matched nothing:
    `access check` reported "no access" for the principal holding the access,
    and everything downstream (gap analysis, `access script`/`approve`) then
    generated a redundant grant. The non-group branch has always matched the
    identifier directly -- the group branch now does the same.
    """
    names = _user_name_sets(user)
    return [lg for lg in logins if principal_matches(lg.name, lg.type, user, names)]


def _user_name_sets(user):
    """The name sets an identifier is matched against, computed once.

    Returned as a dict so the (identical) predicate can be shared by the login
    matcher and the database-principal matcher instead of each growing its own
    copy -- the divergence that produced this whole class of bug.
    """
    group_parts = {_name_part(g).lower() for g in (user.groups or [])}
    group_parts |= {g.lower() for g in (user.groups or [])}   # also bare CNs
    sam = (user.sam_account_name or "").lower()
    login_full = (user.login or "").lower()
    ident = (getattr(user, "identifier", "") or "").lower()
    ident_part = _name_part(ident).lower()
    # The identifier itself, however it was supplied (full DOMAIN\name, bare
    # name, sam, or login). Empty strings are dropped so a blank field cannot
    # match a principal whose name part is also empty.
    self_names = {n for n in (ident, ident_part, sam, login_full,
                              _name_part(login_full).lower()) if n}
    return {"group_parts": group_parts, "self_names": self_names,
            "sam": sam, "login_full": login_full}


def principal_matches(name, type_desc, user, names=None) -> bool:
    """Does `name` (a login OR a database principal) belong to this identifier?

    One predicate, used for both server logins and database principals. A group
    matches by membership *or* by being the identifier itself; an individual
    matches itself. Name-part matched case-insensitively so it is domain-naming
    agnostic.
    """
    names = names or _user_name_sets(user)
    part = _name_part(name).lower()
    full = (name or "").lower()
    if "GROUP" in (type_desc or "").upper():
        return (part in names["group_parts"] or full in names["group_parts"]
                or full in names["self_names"] or part in names["self_names"])
    sam = names["sam"]
    return bool(sam and (part == sam or full == names["login_full"]))


def collect_db_principals(cursor) -> dict:
    """Every database principal, with its auth type and server-login linkage.

    Returns ``{name: {"type": ..., "auth": ..., "has_login": bool}}``. Empty when
    the enriched query is unavailable, which callers must treat as "linkage
    unknown" and fall back to login-only behaviour.
    """
    out = {}
    for r in _fetch(cursor, DB_PRINCIPALS_DETAIL_SQL):
        out[cell(r, "db_user")] = {
            "type": (cell(r, "type_desc") or "").upper(),
            "auth": (_opt(r, "auth_type") or "").upper(),
            "has_login": bool(int(_opt(r, "has_server_login", 0) or 0)),
        }
    return out


def login_less_windows_principals(principals) -> dict:
    """The subset that is LIVE access with no server login.

    Windows users and groups only, and never an ``AUTHENTICATION_TYPE = NONE``
    user (a deliberate ``CREATE USER ... WITHOUT LOGIN``, which no person
    connects as). A login-less SQL_USER is excluded here because it is a broken
    orphan, not access -- ``review.py`` reports that separately.
    """
    return {n: m for n, m in (principals or {}).items()
            if m.get("type") in WINDOWS_PRINCIPAL_TYPES
            and m.get("auth") == AUTH_WINDOWS
            and not m.get("has_login")}


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


def collect_db_access(cursor, server, database, matched_logins, pii_findings,
                      user=None) -> list:
    """Effective access this identifier has in this database.

    Covers both shapes: access reached through a matched server login, and
    access granted directly to a Windows user/group as a database principal with
    no server login. `user` is optional and additive -- omit it and the result is
    exactly the previous login-only behaviour.
    """
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

    ctx = {"roles_by_user": roles_by_user, "perms_by_user": perms_by_user,
           "dbscope_by_user": dbscope_by_user, "pii_by_table": pii_by_table}

    out = []
    seen = set()
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
        via = ("group " + lg.name) if "GROUP" in (lg.type or "").upper() else "direct login"
        if implied != "none":
            granting = ", ".join(_server_grantors(lg))
            if granting:
                via += f" -> server-wide {granting}"
        out.append(_access_row(
            ctx, server, database, principal=db_user or lg.name, db_user=db_user or "",
            login=lg.name, principal_type=(db_users.get(db_user) or lg.type or ""),
            implied=implied, has_server_login=True, via=via))
        if db_user:
            seen.add(db_user.lower())

    # --- database principals with NO server login ---------------------------
    # A Windows user or group can be granted directly in the database with no
    # login at all. The loop above is seeded from logins, so such a principal can
    # never be its subject -- it was unreachable for any identifier, and the
    # access it confers was reported as "no access". `user` is optional so the
    # signature stays backward compatible; without it this degrades to the
    # login-only behaviour.
    if user is not None:
        names = _user_name_sets(user)
        for pname, meta in sorted(login_less_windows_principals(
                collect_db_principals(cursor)).items()):
            if pname.lower() in seen:
                continue
            if not principal_matches(pname, meta["type"], user, names):
                continue
            kind = "group" if meta["type"] == "WINDOWS_GROUP" else "user"
            out.append(_access_row(
                ctx, server, database, principal=pname, db_user=pname, login="",
                principal_type=meta["type"], implied="none", has_server_login=False,
                via=f"{kind} {pname} (database principal; no server login)"))
            seen.add(pname.lower())
    return out


def _access_row(ctx, server, database, *, principal, db_user, login,
                principal_type, implied, has_server_login, via) -> DatabaseAccess:
    """Level, PII exposure and escalation flags for one principal.

    Shared by the login-backed and the login-less paths so both are levelled by
    identical rules -- the two must never drift.
    """
    roles = sorted(ctx["roles_by_user"].get(db_user, [])) if db_user else []
    perms = ctx["perms_by_user"].get(db_user, []) if db_user else []
    # Database-scoped grants (CONTROL ON DATABASE) apply to everything in the
    # database and carry no object, so they are levelled separately.
    scoped = ctx["dbscope_by_user"].get(db_user, []) if db_user else []
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
    pii_by_table = ctx["pii_by_table"]
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

    return DatabaseAccess(
        server=server, database=database, login=login, db_user=db_user,
        via=via, roles=roles, permissions=perms, level=level,
        pii_tables=pii_tables, flags=sorted(set(flags)),
        principal=principal, principal_type=(principal_type or "").upper(),
        has_server_login=has_server_login)
