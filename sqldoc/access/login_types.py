"""SQL Server login-type classification + correct CREATE LOGIN/USER syntax.

Handles every login pattern an enterprise runs into:

* **windows** — Windows group *or* individual login (``CREATE LOGIN [x] FROM WINDOWS``);
* **sql** — SQL Server native login (``CREATE LOGIN [x] WITH PASSWORD = ...``);
* **azure_ad** — Azure AD / Entra principal, modern auth
  (``... FROM EXTERNAL PROVIDER``); on Azure SQL Database these are *contained*
  users created directly in the database with no server login;
* **managed_identity** — an Azure managed identity, also an external provider.

Mixed-mode instances are handled naturally: each login is classified on its own.
"""
WINDOWS = "windows"
SQL = "sql"
AZURE_AD = "azure_ad"
MANAGED_IDENTITY = "managed_identity"

_HINTS = {
    "windows": WINDOWS, "windows_group": WINDOWS, "windows_login": WINDOWS, "ad": WINDOWS,
    "sql": SQL, "sql_login": SQL, "native": SQL,
    "azure_ad": AZURE_AD, "azuread": AZURE_AD, "aad": AZURE_AD, "entra": AZURE_AD,
    "external": AZURE_AD, "external_provider": AZURE_AD,
    "managed_identity": MANAGED_IDENTITY, "mi": MANAGED_IDENTITY, "msi": MANAGED_IDENTITY,
}

_LABELS = {
    WINDOWS: "Windows login", SQL: "SQL Server login",
    AZURE_AD: "Azure AD (external provider)", MANAGED_IDENTITY: "Managed identity",
}

# SQL Server family dialects where FROM EXTERNAL PROVIDER at the server level works
# (Managed Instance, on-prem talking to AAD). Azure SQL Database is contained-only.
_CONTAINED_ONLY = {"azuresql"}


def _q(name: str) -> str:
    return "[" + (name or "").replace("]", "]]") + "]"


def classify_login(name: str, hint=None) -> str:
    """Classify a login by explicit hint, else by name shape."""
    if hint:
        h = str(hint).strip().lower()
        if h in _HINTS:
            return _HINTS[h]
    name = name or ""
    if "\\" in name:
        return WINDOWS
    if "@" in name:
        return AZURE_AD
    return SQL


def is_external(ltype: str) -> bool:
    return ltype in (AZURE_AD, MANAGED_IDENTITY)


def label(ltype: str) -> str:
    return _LABELS.get(ltype, ltype)


def needs_server_login(ltype: str, dialect: str = "sqlserver") -> bool:
    """False for Azure SQL Database external users (contained — no server login)."""
    if is_external(ltype) and dialect in _CONTAINED_ONLY:
        return False
    return True


def create_login_sql(name: str, ltype: str, dialect: str = "sqlserver") -> str:
    q = _q(name)
    if ltype == WINDOWS:
        return f"CREATE LOGIN {q} FROM WINDOWS;"
    if is_external(ltype):
        return f"CREATE LOGIN {q} FROM EXTERNAL PROVIDER;"
    return (f"-- SQL login: set a strong password out-of-band, do not commit it.\n"
            f"    CREATE LOGIN {q} WITH PASSWORD = N'<set-a-strong-password>';")


def create_user_sql(name: str, ltype: str, dialect: str = "sqlserver") -> str:
    q = _q(name)
    if is_external(ltype) and not needs_server_login(ltype, dialect):
        # Azure SQL Database contained external user.
        return f"CREATE USER {q} FROM EXTERNAL PROVIDER;"
    return f"CREATE USER {q} FOR LOGIN {q};"
