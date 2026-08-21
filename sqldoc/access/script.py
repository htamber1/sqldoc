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


def _lit(value: str) -> str:
    """Escape a value for use inside a T-SQL string literal (double single
    quotes). Defense-in-depth so a login name containing a quote cannot break
    out of the N'...' existence-check into the generated grant script."""
    return (value or "").replace("'", "''")


def pick_login(report, parsed, override=None):
    """Choose the login to grant to. Returns (login_name, uses_windows_group, note)."""
    user = report.user
    if override:
        return override, ("\\" in override), "caller-specified login"

    from sqldoc.access.roles import level_meets
    needs = parsed.level or "read"
    dbl = (parsed.database or "").lower()
    # 1) A Windows group already present in the target database — upgrade it.
    #    Skip any group that ALREADY satisfies the requested level: granting to it
    #    changes nothing for the requester while widening that group's footprint
    #    for every other member. That matters most when the group is privileged —
    #    picking a sysadmin group as the target of a read request would add a
    #    redundant role membership to the most powerful group on the server.
    for a in report.access:
        if (a.database or "").lower() == dbl and "group" in (a.via or "").lower():
            if level_meets(a.level, needs):
                continue
            # A group granted only as a database principal has no server login
            # to grant to, and `a.login` is empty for it. Returning it here would
            # emit CREATE LOGIN [group] FROM WINDOWS -- materialising a server
            # login for a group the shop deliberately keeps login-less, i.e.
            # changing their access model as a side effect of a grant request.
            # Skip it and let a real login be chosen.
            if not getattr(a, "has_server_login", True) or not a.login:
                continue
            return a.login, True, f"existing group with access to {a.database}"
    # 2) A Windows group login the user belongs to that exists on the server —
    #    again skipping any that is already server-wide privileged (sysadmin /
    #    CONTROL SERVER), which the requested level cannot add to.
    from sqldoc.access.sqlserver import _server_implied_level
    self_name = (user.login or user.sam_account_name or user.identifier or "").lower()
    for lg in report.logins:
        if "GROUP" in (lg.type or "").upper():
            if level_meets(_server_implied_level(lg), needs):
                continue
            # The requested principal may BE this group: estates that grant to
            # role groups ask for the group by name. Saying "a group the user
            # belongs to" would misdescribe that, so name the case correctly.
            if lg.name.lower() == self_name:
                return lg.name, True, "granting directly to the requested AD group"
            return lg.name, True, "existing AD group login the user belongs to"
    # 3) The identifier is itself a Windows group: grant to it directly.
    #    Estates that standardise on role groups request access *for the group*,
    #    so telling them "no suitable AD group found — consider creating a
    #    role-based AD group" is both wrong and the opposite of their policy.
    login = user.login or user.sam_account_name or user.identifier
    for lg in report.logins:
        if "GROUP" in (lg.type or "").upper() and lg.name.lower() == (login or "").lower():
            return lg.name, True, "granting directly to the requested AD group"

    # 4) Fall back to the user's own Windows login.
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
        grant.append(f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'{_lit(login)}')")  # nosec B608 - generated review script; login single-quote-escaped via _lit(), identifiers bracket-quoted via _q()
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
    grant.append(f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'{_lit(login)}')")  # nosec B608 - generated review script; login single-quote-escaped via _lit(), identifiers bracket-quoted via _q()
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
