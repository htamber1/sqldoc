"""Security vulnerability scanner across dialects.

Runs dialect-specific hardening checks and rolls them up into ONE 0-100 security
score with consistent HIGH / MEDIUM / LOW findings, so the same report shape
works whether the target is SQL Server, PostgreSQL, or MySQL.

* **SQL Server** — SA account enabled, ``xp_cmdshell``, ``TRUSTWORTHY``
  databases, blank/weak login passwords, and object grants to ``public``.
* **PostgreSQL** — extra login-capable superusers, risky ``pg_hba.conf`` rules
  (``trust`` / plaintext ``password``), ``CREATE`` on the ``public`` schema,
  ``ssl`` off, and the default ``postgres`` account.
* **MySQL** — anonymous accounts, remote ``root`` login, accounts with no
  password, the ``FILE`` privilege, and an unrestricted ``secure_file_priv``.

Reads only server configuration + catalog metadata — never table row data.
"""
from dataclasses import dataclass, field

from sqldoc.dbutil import cell

SECURE_DIALECTS = {"sqlserver", "azuresql", "postgres", "mysql"}

_SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_SEVERITY_WEIGHT = {"HIGH": 15, "MEDIUM": 7, "LOW": 3}


def _s(v) -> str:
    return "" if v is None else str(v)


@dataclass
class SecurityFinding:
    severity: str        # HIGH / MEDIUM / LOW
    category: str
    title: str
    detail: str = ""
    recommendation: str = ""


@dataclass
class SecurityReport:
    dialect: str = ""
    supported: bool = True
    findings: list = field(default_factory=list)
    checks_run: int = 0
    errors: list = field(default_factory=list)

    @property
    def score(self) -> int:
        penalty = sum(_SEVERITY_WEIGHT.get(f.severity, 0) for f in self.findings)
        return max(0, 100 - penalty)

    @property
    def grade(self) -> str:
        sc = self.score
        return "A" if sc >= 90 else "B" if sc >= 75 else "C" if sc >= 60 else "D" if sc >= 40 else "F"


class _Collector:
    def __init__(self, cursor, report):
        self.cursor = cursor
        self.report = report

    def add(self, severity, category, title, detail="", recommendation=""):
        self.report.findings.append(SecurityFinding(severity, category, title, detail, recommendation))

    def check(self, label, fn):
        self.report.checks_run += 1
        try:
            fn()
        except Exception as e:
            self.report.errors.append((label, f"{type(e).__name__}: {e}"))


# --- SQL Server ------------------------------------------------------------

def _scan_sqlserver(c: _Collector):
    def logins():
        c.cursor.execute("""
            SELECT name, is_disabled,
                   CASE WHEN PWDCOMPARE('', password_hash) = 1 THEN 1 ELSE 0 END AS blank_pw
            FROM sys.sql_logins
        """)
        for r in c.cursor.fetchall():
            name = _s(cell(r, "name"))
            if name.lower() == "sa" and not int(cell(r, "is_disabled") or 0):
                c.add("MEDIUM", "Accounts", "SA account is enabled",
                      "The built-in 'sa' login is enabled and is a common brute-force target.",
                      "Disable 'sa' (or rename it) and use a dedicated admin login.")
            if int(cell(r, "blank_pw") or 0):
                c.add("HIGH", "Passwords", f"Login '{name}' has a blank password",
                      "A SQL login authenticates with an empty password.",
                      "Set a strong password or disable the login.")

    def config():
        c.cursor.execute("""
            SELECT name, CAST(value_in_use AS int) AS v
            FROM sys.configurations
            WHERE name IN ('xp_cmdshell', 'clr enabled', 'Ad Hoc Distributed Queries', 'Ole Automation Procedures')
        """)
        for r in c.cursor.fetchall():
            name, v = _s(cell(r, "name")), int(cell(r, "v") or 0)
            if name == "xp_cmdshell" and v:
                c.add("HIGH", "Surface area", "xp_cmdshell is enabled",
                      "xp_cmdshell lets SQL execute OS shell commands.",
                      "Disable xp_cmdshell unless it is strictly required.")
            elif name == "Ole Automation Procedures" and v:
                c.add("MEDIUM", "Surface area", "OLE Automation Procedures enabled",
                      "sp_OA* procedures can run arbitrary COM objects.",
                      "Disable unless required.")
            elif name == "Ad Hoc Distributed Queries" and v:
                c.add("MEDIUM", "Surface area", "Ad Hoc Distributed Queries enabled",
                      "OPENROWSET/OPENDATASOURCE ad-hoc access is on.",
                      "Disable unless required.")

    def trustworthy():
        c.cursor.execute("""
            SELECT name FROM sys.databases
            WHERE is_trustworthy_on = 1 AND name <> 'msdb'
        """)
        for r in c.cursor.fetchall():
            c.add("HIGH", "Configuration", f"TRUSTWORTHY is ON for '{_s(cell(r, 'name'))}'",
                  "TRUSTWORTHY + a db_owner can escalate to sysadmin.",
                  "Set TRUSTWORTHY OFF and use module signing instead.")

    def public_perms():
        c.cursor.execute("""
            SELECT COUNT(*) AS n
            FROM sys.database_permissions
            WHERE grantee_principal_id = DATABASE_PRINCIPAL_ID('public')
              AND state_desc = 'GRANT' AND class = 1
        """)
        rows = c.cursor.fetchall()
        n = int(cell(rows[0], "n") or 0) if rows else 0
        if n:
            c.add("MEDIUM", "Permissions", f"public role has {n} object grant(s)",
                  "Object permissions granted to public apply to every user.",
                  "Revoke object grants from public; grant to specific roles.")

    c.check("SQL logins", logins)
    c.check("Server configuration", config)
    c.check("TRUSTWORTHY databases", trustworthy)
    c.check("public permissions", public_perms)


# --- PostgreSQL ------------------------------------------------------------

def _scan_postgres(c: _Collector):
    def superusers():
        c.cursor.execute("SELECT rolname FROM pg_roles WHERE rolsuper AND rolcanlogin ORDER BY rolname")
        supers = [_s(cell(r, "rolname")) for r in c.cursor.fetchall()]
        extra = [s for s in supers if s != "postgres"]
        if extra:
            c.add("MEDIUM", "Accounts", f"{len(extra)} extra login-capable superuser(s)",
                  "Superuser roles bypass all permission checks: " + ", ".join(extra),
                  "Grant only the privileges needed; avoid login-capable superusers.")
        if "postgres" in supers:
            c.add("LOW", "Accounts", "Default 'postgres' superuser can log in",
                  "The default postgres superuser is login-capable.",
                  "Ensure it has a strong, non-default password (or disable direct login).")

    def pg_hba():
        c.cursor.execute("""
            SELECT type, database, user_name, address, auth_method
            FROM pg_hba_file_rules
        """)
        for r in c.cursor.fetchall():
            method = _s(cell(r, "auth_method")).lower()
            who = f"{_s(cell(r,'user_name'))}@{_s(cell(r,'address')) or 'local'}"
            if method == "trust":
                c.add("HIGH", "Authentication", "pg_hba.conf 'trust' rule",
                      f"A trust rule authenticates without a password ({who}).",
                      "Replace 'trust' with scram-sha-256 (or md5).")
            elif method == "password":
                c.add("MEDIUM", "Authentication", "pg_hba.conf plaintext 'password' rule",
                      f"'password' sends the password in clear text ({who}).",
                      "Use scram-sha-256 instead of 'password'.")

    def public_schema():
        c.cursor.execute("SELECT has_schema_privilege('public', 'public', 'CREATE') AS pub_create")
        rows = c.cursor.fetchall()
        if rows and bool(cell(rows[0], "pub_create")):
            c.add("MEDIUM", "Permissions", "public role can CREATE in the public schema",
                  "Any user can create objects in the public schema.",
                  "REVOKE CREATE ON SCHEMA public FROM PUBLIC.")

    def ssl():
        c.cursor.execute("SELECT setting FROM pg_settings WHERE name = 'ssl'")
        rows = c.cursor.fetchall()
        if rows and _s(cell(rows[0], "setting")).lower() == "off":
            c.add("MEDIUM", "Encryption", "SSL/TLS is disabled",
                  "Connections are not encrypted (ssl = off).",
                  "Enable ssl and require encrypted connections.")

    c.check("Superusers", superusers)
    c.check("pg_hba.conf", pg_hba)
    c.check("public schema", public_schema)
    c.check("SSL", ssl)


# --- MySQL -----------------------------------------------------------------

def _scan_mysql(c: _Collector):
    def users():
        c.cursor.execute("SELECT user, host, authentication_string, plugin FROM mysql.user")
        for r in c.cursor.fetchall():
            user = _s(cell(r, "user"))
            host = _s(cell(r, "host"))
            auth = _s(cell(r, "authentication_string"))
            plugin = _s(cell(r, "plugin"))
            if user == "":
                c.add("HIGH", "Accounts", f"Anonymous account exists ('@{host}')",
                      "Anonymous accounts let anyone connect without a username.",
                      "DROP the anonymous accounts.")
            if user == "root" and host not in ("localhost", "127.0.0.1", "::1"):
                c.add("HIGH", "Accounts", f"root can log in remotely (root@{host})",
                      "Remote root login is a major attack surface.",
                      "Restrict root to localhost.")
            if not auth and plugin not in ("auth_socket", "mysql_no_login", "unix_socket"):
                c.add("HIGH", "Passwords", f"Account '{user}@{host}' has no password",
                      "A login account authenticates with no password.",
                      "Set a strong password or remove the account.")

    def file_priv():
        c.cursor.execute("""
            SELECT grantee FROM information_schema.user_privileges
            WHERE privilege_type = 'FILE'
        """)
        grantees = [_s(cell(r, "grantee")) for r in c.cursor.fetchall()]
        non_root = [g for g in grantees if "root" not in g.lower()]
        if non_root:
            c.add("MEDIUM", "Permissions", f"FILE privilege granted to {len(non_root)} non-root account(s)",
                  "FILE can read/write server files (LOAD_FILE, INTO OUTFILE): " + ", ".join(non_root[:5]),
                  "Revoke FILE from application accounts.")

    def secure_file_priv():
        c.cursor.execute("SELECT @@secure_file_priv AS sfp")
        rows = c.cursor.fetchall()
        sfp = cell(rows[0], "sfp") if rows else None
        if sfp is None or _s(sfp) == "":
            c.add("MEDIUM", "Configuration", "secure_file_priv is unrestricted",
                  "secure_file_priv is empty/NULL, so file import/export is not confined to a directory.",
                  "Set secure_file_priv to a dedicated directory.")

    c.check("User accounts", users)
    c.check("FILE privilege", file_priv)
    c.check("secure_file_priv", secure_file_priv)


# --- dispatch --------------------------------------------------------------

def collect_security(adapter) -> SecurityReport:
    dialect = getattr(adapter, "dialect", "sqlserver")
    report = SecurityReport(dialect=dialect)
    if dialect not in SECURE_DIALECTS:
        report.supported = False
        report.errors.append(("Unsupported", f"Security scanning is not implemented for {dialect}."))
        return report
    conn = adapter.connect()
    cursor = adapter.cursor(conn)
    collector = _Collector(cursor, report)
    try:
        if dialect in ("sqlserver", "azuresql"):
            _scan_sqlserver(collector)
        elif dialect == "postgres":
            _scan_postgres(collector)
        elif dialect == "mysql":
            _scan_mysql(collector)
    finally:
        conn.close()
    report.findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.category, f.title))
    return report


def summarize(report: SecurityReport) -> dict:
    return {
        "score": report.score,
        "grade": report.grade,
        "high": sum(1 for f in report.findings if f.severity == "HIGH"),
        "medium": sum(1 for f in report.findings if f.severity == "MEDIUM"),
        "low": sum(1 for f in report.findings if f.severity == "LOW"),
        "checks_run": report.checks_run,
        "degraded": len(report.errors),
    }
