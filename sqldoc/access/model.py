"""Shared dataclasses for the `sqldoc access` command suite.

These flow through the whole access pipeline: AD lookup populates :class:`ADUser`,
the SQL Server probe populates :class:`Login`/:class:`DatabaseAccess`, the checker
assembles an :class:`AccessReport`, and the request/script/review commands read
and extend them. Dialect-neutral currency, mirroring the extractor's dataclasses.
"""
from dataclasses import dataclass, field

# Coarse access levels, ordered. Mirrors comply.LEVELS so classifications line up.
LEVELS = ["none", "read", "write", "admin"]
LEVEL_ORDER = {l: i for i, l in enumerate(LEVELS)}


@dataclass
class ADUser:
    """A resolved Active Directory / Entra ID user."""
    identifier: str                 # what was searched for
    display_name: str = ""
    sam_account_name: str = ""      # e.g. jsmith
    user_principal_name: str = ""   # e.g. jsmith@corp.com
    email: str = ""
    distinguished_name: str = ""
    login: str = ""                 # DOMAIN\jsmith (for on-prem)
    title: str = ""
    department: str = ""
    enabled: bool = True
    groups: list = field(default_factory=list)   # AD group names (DOMAIN\Group or CN)
    source: str = ""                # ldap | graph
    found: bool = True


@dataclass
class Login:
    """A SQL Server server-level principal (login)."""
    name: str                       # DOMAIN\Group, DOMAIN\user, or a SQL login name
    type: str = ""                  # WINDOWS_GROUP | WINDOWS_LOGIN | SQL_LOGIN | ...
    is_disabled: bool = False
    server: str = ""
    server_roles: list = field(default_factory=list)   # fixed server roles held


@dataclass
class DatabaseAccess:
    """Effective access one principal (login) has in one database."""
    server: str
    database: str
    login: str                      # the server login granting this access
    db_user: str = ""               # the mapped database principal name
    via: str = ""                   # "group DOMAIN\SalesRead" | "direct login"
    roles: list = field(default_factory=list)          # database roles held
    permissions: list = field(default_factory=list)    # (permission, state, schema, object)
    level: str = "none"             # coarse effective level: read/write/admin
    pii_tables: list = field(default_factory=list)     # [(schema, table, risk, [regs])]


@dataclass
class AccessReport:
    """Everything the check command surfaces for one user."""
    user: ADUser
    logins: list = field(default_factory=list)         # matched Login rows
    access: list = field(default_factory=list)         # DatabaseAccess rows
    matched_groups: list = field(default_factory=list) # AD groups that map to a login
    errors: list = field(default_factory=list)         # (label, message) best-effort notes

    def has_any_access(self) -> bool:
        return any(a.level != "none" or a.roles or a.permissions for a in self.access)


@dataclass
class ParsedRequest:
    """A plain-English access request parsed into structured intent."""
    raw: str
    database: str = ""
    schema: str = ""                # optional; "" = whole database
    level: str = "read"             # read | write | admin
    objects: list = field(default_factory=list)   # optional specific tables
    confidence: float = 0.0
    note: str = ""


@dataclass
class GapResult:
    """Outcome of comparing a request against current access."""
    verdict: str                    # ALREADY | PARTIAL | NONE
    request: ParsedRequest = None
    have_level: str = "none"
    needs_level: str = "read"
    explanation: str = ""
    missing: list = field(default_factory=list)     # human-readable missing items
    current: list = field(default_factory=list)     # human-readable current items


@dataclass
class GeneratedScript:
    server: str = ""
    database: str = ""
    grant_sql: str = ""
    rollback_sql: str = ""
    impact: list = field(default_factory=list)      # objects that become accessible
    pii_exposed: list = field(default_factory=list) # (schema, table, risk, [regs])
    login_name: str = ""
    role: str = ""
    uses_windows_group: bool = False
    note: str = ""


@dataclass
class ReviewFinding:
    category: str                   # inactive | over_privileged | sod | orphaned | service_account
    severity: str                   # HIGH | MEDIUM | LOW
    principal: str
    server: str = ""
    database: str = ""
    summary: str = ""
    detail: str = ""
    fix_sql: str = ""


@dataclass
class RoleRecommendation:
    user: ADUser = None
    database: str = ""
    recommended_roles: list = field(default_factory=list)   # [(role, reason)]
    peers_considered: int = 0
    rationale: str = ""
    least_privilege_note: str = ""
