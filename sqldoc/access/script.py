"""Generate the SQL to grant a requested access, plus a matching rollback.

Follows SQL Server best practices:

* prefer a **Windows group login** over an individual login where one is
  available (existing group the user belongs to, or a caller override);
* **check-then-create** the login and the database user (idempotent);
* add to the **least-privilege** fixed role(s) for the requested level;
* every statement is **commented**; a **rollback** script undoes exactly the
  membership added (and offers optional user/login cleanup);
* an **impact analysis** lists what becomes accessible, flagging PII tables.
"""
from sqldoc.access.model import GeneratedScript
from sqldoc.access.roles import roles_for_level


def _q(name: str) -> str:
    """Bracket-quote an identifier, escaping any embedded ]."""
    return "[" + (name or "").replace("]", "]]") + "]"


def pick_login(report, parsed, override=None):
    """Choose the login to grant to. Returns (login_name, uses_windows_group, note)."""
    user = report.user
    if override:
        return override, ("\\" in override), "caller-specified login"

    dbl = (parsed.database or "").lower()
    # 1) A Windows group already present in the target database — upgrade it.
    for a in report.access:
        if (a.database or "").lower() == dbl and "group" in (a.via or "").lower():
            return a.login, True, f"existing group with access to {a.database}"
    # 2) A Windows group login the user belongs to that exists on the server.
    for lg in report.logins:
        if "GROUP" in (lg.type or "").upper():
            return lg.name, True, "existing AD group login the user belongs to"
    # 3) Fall back to the user's own Windows login.
    login = user.login or user.sam_account_name or user.identifier
    return login, False, ("no suitable AD group found — using an individual login; "
                          "consider creating a role-based AD group instead")


def _already_roles(report, database) -> set:
    dbl = (database or "").lower()
    roles = set()
    for a in report.access:
        if (a.database or "").lower() == dbl:
            roles.update(a.roles)
    return roles


def generate_script(report, parsed, server, database, tables=None, pii_findings=None,
                    login_override=None, dialect="sqlserver", login_type=None) -> GeneratedScript:
    """Produce the grant + rollback scripts and impact analysis for one request.

    ``login_type`` (windows / sql / azure_ad / managed_identity) is honoured when
    given, else classified from the login name — driving the correct CREATE LOGIN
    / CREATE USER syntax for every login pattern (incl. Azure AD external
    providers and Azure SQL Database contained users)."""
    from sqldoc.access import login_types as lt
    login, is_group, strategy = pick_login(report, parsed, override=login_override)
    ltype = lt.classify_login(login, hint=login_type)
    needs = parsed.level or "read"
    target_roles = roles_for_level(needs)
    already = _already_roles(report, database)
    add_roles = [r for r in target_roles if r not in already]

    gs = GeneratedScript(server=server, database=database, login_name=login,
                         role=", ".join(add_roles), uses_windows_group=is_group)
    gs.login_type = ltype

    if not add_roles:
        gs.note = (f"No changes needed: the grantee already holds {', '.join(sorted(already)) or 'the required roles'} "
                   f"in {database}, which satisfies {needs} access.")
        gs.grant_sql = f"-- No changes required for {needs} access to {database}.\n"
        gs.rollback_sql = "-- Nothing to roll back.\n"
        return gs

    ql = _q(login)
    kind_label = ("Windows group" if is_group else lt.label(ltype))
    server_login = lt.needs_server_login(ltype, dialect)

    grant = []
    grant.append("-- sqldoc access grant script")
    grant.append(f"-- Server:   {server}")
    grant.append(f"-- Database: {database}")
    grant.append(f"-- Grantee:  {login}  ({kind_label})")
    grant.append(f"-- Level:    {needs}  ->  role(s): {', '.join(add_roles)}")
    grant.append(f"-- Strategy: {strategy}")
    grant.append("-- Review before running. A matching rollback script is provided below.")
    grant.append("")
    step = 1
    if server_login:
        grant.append(f"-- {step}) Ensure the server login exists (created only if missing).")
        grant.append("USE [master];")
        grant.append("GO")
        grant.append(f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'{login}')")
        grant.append("BEGIN")
        grant.append(f"    {lt.create_login_sql(login, ltype, dialect)}")
        grant.append("END")
        grant.append("GO")
        grant.append("")
        step += 1
    else:
        grant.append(f"-- Azure SQL Database: {lt.label(ltype)} is a contained user "
                     "(no server login needed).")
        grant.append("")
    grant.append(f"-- {step}) Ensure the database user exists.")
    grant.append(f"USE {_q(database)};")
    grant.append("GO")
    grant.append(f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'{login}')")
    grant.append("BEGIN")
    grant.append(f"    {lt.create_user_sql(login, ltype, dialect)}")
    grant.append("END")
    grant.append("GO")
    grant.append("")
    step += 1
    grant.append(f"-- {step}) Grant {needs} access via the least-privilege fixed role(s).")
    for r in add_roles:
        grant.append(f"ALTER ROLE {_q(r)} ADD MEMBER {ql};")
    grant.append("GO")
    gs.grant_sql = "\n".join(grant) + "\n"

    roll = []
    roll.append("-- sqldoc access ROLLBACK script")
    roll.append(f"-- Undoes the grant above on {server} / {database}.")
    roll.append(f"USE {_q(database)};")
    roll.append("GO")
    roll.append("-- Remove the role membership(s) this grant added.")
    for r in reversed(add_roles):
        roll.append(f"ALTER ROLE {_q(r)} DROP MEMBER {ql};")
    roll.append("GO")
    roll.append("-- Optional: if the user/login were created *only* for this grant, drop them:")
    roll.append(f"-- DROP USER {ql};")
    if server_login:
        roll.append(f"-- USE [master]; DROP LOGIN {ql};")
    roll.append("GO")
    gs.rollback_sql = "\n".join(roll) + "\n"

    # Impact analysis: new roles are database-wide, so every table becomes
    # readable (and writable at write/admin). Flag any PII tables among them.
    tables = tables or []
    gs.impact = sorted(f"{t.schema}.{t.name}" for t in tables)
    pii_by = {}
    for f in (pii_findings or []):
        key = (f.schema, f.table)
        info = pii_by.setdefault(key, {"risk": f.risk, "regs": set()})
        info["regs"].update(f.regulations or [])
    gs.pii_exposed = [(s, t, i["risk"], sorted(i["regs"])) for (s, t), i in sorted(pii_by.items())]
    gs.note = (f"Grants {needs} access to {database} for {login}. "
               f"{len(gs.impact)} object(s) become accessible; "
               f"{len(gs.pii_exposed)} carry PII.")
    return gs
