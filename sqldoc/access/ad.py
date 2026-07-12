"""Active Directory / Entra ID lookup for the access suite.

Two interchangeable back-ends behind a common :class:`ADSource` interface:

* **LDAP** (``ldap3`` — pure Python, no native deps) for on-premise AD;
* **Microsoft Graph** (MSAL) for Azure AD / Entra ID.

``get_source(config)`` picks one from ``access.ad.type`` (ldap | graph | auto).
``auto`` chooses Graph when the config looks cloud (a ``tenant_id`` /
``*.onmicrosoft.com`` domain), else LDAP. The connection factory and every HTTP
call are module-level so tests inject fakes without ldap3 / MSAL / a network.
"""
from sqldoc.access.model import ADUser
from sqldoc.integrations.base import IntegrationError, require


def get_source(config: dict):
    """Build the configured AD source. Raises IntegrationError on bad config."""
    cfg = config or {}
    kind = (cfg.get("type") or "auto").lower()
    if kind == "auto":
        kind = "graph" if _looks_cloud(cfg) else "ldap"
    if kind == "ldap":
        return LdapADSource(cfg)
    if kind in ("graph", "azuread", "entra", "entraid"):
        return GraphADSource(cfg)
    raise IntegrationError(f"Unknown access.ad.type '{kind}' (use ldap | graph | auto).")


def _looks_cloud(cfg) -> bool:
    if cfg.get("tenant_id") or cfg.get("client_secret") and cfg.get("client_id"):
        return True
    domain = str(cfg.get("domain") or cfg.get("base_dn") or "")
    return "onmicrosoft.com" in domain.lower()


def _cn(dn: str) -> str:
    """Extract the CN (group/first RDN value) from a distinguished name."""
    if not dn:
        return ""
    first = dn.split(",", 1)[0]
    return first.split("=", 1)[1] if "=" in first else first


# --- LDAP (ldap3) ----------------------------------------------------------

def build_connection(cfg: dict):
    """Open + bind an ldap3 connection (module-level for mocking)."""
    ldap3 = require("ldap3", "activedirectory")
    server = ldap3.Server(cfg["server"], get_info=ldap3.ALL, use_ssl=bool(cfg.get("use_ssl", False)))
    conn = ldap3.Connection(
        server, user=cfg.get("bind_dn"), password=cfg.get("bind_password"),
        auto_bind=True)
    return conn


_LDAP_ATTRS = ["displayName", "sAMAccountName", "userPrincipalName", "mail",
               "distinguishedName", "memberOf", "title", "department", "userAccountControl"]


def _attr(entry, name, default=""):
    """Read one attribute value from an ldap3 entry (tolerant of shapes)."""
    try:
        v = getattr(entry, name)
        v = getattr(v, "value", v)
    except Exception:
        try:
            v = entry[name]
        except Exception:
            return default
    if v is None:
        return default
    return v


class LdapADSource:
    source = "ldap"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def _need(self):
        missing = [k for k in ("server", "base_dn") if not self.cfg.get(k)]
        if missing:
            raise IntegrationError(
                f"access.ad (ldap) is missing: {', '.join(missing)}. "
                f"Set server, base_dn (and bind_dn/bind_password) under access.ad.")

    def get_user(self, identifier: str) -> ADUser:
        self._need()
        conn = build_connection(self.cfg)
        ident = _ldap_escape(identifier)
        flt = (f"(&(objectClass=user)(|(sAMAccountName={ident})"
               f"(mail={ident})(userPrincipalName={ident})))")
        # 'SUBTREE' is ldap3's own value for the constant, so we avoid importing
        # the package here (build_connection already requires it in real runs).
        conn.search(self.cfg["base_dn"], flt, search_scope="SUBTREE",
                    attributes=_LDAP_ATTRS)
        entries = list(getattr(conn, "entries", []) or [])
        if not entries:
            return ADUser(identifier=identifier, found=False, source=self.source)
        e = entries[0]
        member_of = _attr(e, "memberOf", [])
        if isinstance(member_of, str):
            member_of = [member_of]
        groups = [_cn(dn) for dn in member_of if dn]
        sam = str(_attr(e, "sAMAccountName"))
        uac = _attr(e, "userAccountControl", 0)
        try:
            disabled = bool(int(uac) & 0x2)
        except (TypeError, ValueError):
            disabled = False
        domain = self.cfg.get("netbios_domain") or self.cfg.get("domain", "")
        login = f"{domain}\\{sam}" if domain and sam else sam
        return ADUser(
            identifier=identifier, display_name=str(_attr(e, "displayName")) or sam,
            sam_account_name=sam, user_principal_name=str(_attr(e, "userPrincipalName")),
            email=str(_attr(e, "mail")), distinguished_name=str(_attr(e, "distinguishedName")),
            login=login, title=str(_attr(e, "title")), department=str(_attr(e, "department")),
            enabled=not disabled, groups=groups, source=self.source, found=True)


def _ldap_escape(s: str) -> str:
    for ch, rep in (("\\", "\\5c"), ("*", "\\2a"), ("(", "\\28"), (")", "\\29"), ("\x00", "\\00")):
        s = s.replace(ch, rep)
    return s


# --- Microsoft Graph (Entra ID) --------------------------------------------

_GRAPH = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


def acquire_token(cfg: dict) -> str:
    msal = require("msal", "activedirectory")
    app = msal.ConfidentialClientApplication(
        client_id=cfg["client_id"],
        authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
        client_credential=cfg["client_secret"])
    res = app.acquire_token_for_client(scopes=_GRAPH_SCOPE)
    if "access_token" not in res:
        raise IntegrationError("Entra ID auth failed: "
                               + res.get("error_description", res.get("error", "no token")))
    return res["access_token"]


def graph_get(path: str, token: str, *, timeout: float = 30.0, params=None):
    import requests
    resp = requests.get(f"{_GRAPH}{path}", headers={"Authorization": f"Bearer {token}"},
                        params=params, timeout=timeout)
    if resp.status_code == 404:
        return None
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Graph GET {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json()


class GraphADSource:
    source = "graph"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def _need(self):
        missing = [k for k in ("tenant_id", "client_id", "client_secret") if not self.cfg.get(k)]
        if missing:
            raise IntegrationError(
                f"access.ad (graph) is missing: {', '.join(missing)}. "
                f"Set tenant_id, client_id, client_secret under access.ad.")

    def get_user(self, identifier: str) -> ADUser:
        self._need()
        token = acquire_token(self.cfg)
        sel = ("displayName,userPrincipalName,mail,jobTitle,department,accountEnabled,"
               "onPremisesSamAccountName")
        u = graph_get(f"/users/{identifier}", token, params={"$select": sel})
        if u is None:
            return ADUser(identifier=identifier, found=False, source=self.source)
        groups = self._groups(token, identifier)
        sam = u.get("onPremisesSamAccountName") or (u.get("userPrincipalName", "").split("@")[0])
        return ADUser(
            identifier=identifier, display_name=u.get("displayName") or sam,
            sam_account_name=sam, user_principal_name=u.get("userPrincipalName", ""),
            email=u.get("mail") or u.get("userPrincipalName", ""),
            login=sam, title=u.get("jobTitle") or "", department=u.get("department") or "",
            enabled=bool(u.get("accountEnabled", True)), groups=groups,
            source=self.source, found=True)

    def _groups(self, token, identifier) -> list:
        data = graph_get(f"/users/{identifier}/transitiveMemberOf", token,
                         params={"$select": "displayName", "$top": 999})
        out = []
        for g in (data or {}).get("value", []):
            name = g.get("displayName")
            if name:
                out.append(name)
        return out
