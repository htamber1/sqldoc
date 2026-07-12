"""Identity-provider lookup for the access suite.

Interchangeable back-ends behind a common :class:`ADSource` interface, so the
same access commands work in every enterprise identity environment:

* **LDAP** (``ldap3`` — pure Python) for on-premise AD *and* generic non-Microsoft
  directories (OpenLDAP, 389-DS, …) via configurable attribute mapping;
* **Microsoft Graph** (MSAL) for Azure AD / Entra ID, including **hybrid**
  (on-prem AD synced to Entra — reads onPremises attributes);
* **Okta** Universal Directory (REST API + SSWS token);
* **Google Workspace** directory (Admin SDK Directory API + service account);
* **JumpCloud** (JumpCloud API + x-api-key);
* **native** — no directory at all (pure SQL Server logins), so access commands
  still run and match on SQL-native login names.

``get_source(config)`` picks one from ``access.ad.type`` or auto-detects from the
config shape. Every connection factory / HTTP call is module-level so tests
inject fakes without any SDK or network.
"""
from sqldoc.access.model import ADUser
from sqldoc.integrations.base import IntegrationError, require

_ALIASES = {
    "ad": "ldap", "active-directory": "ldap", "openldap": "generic-ldap",
    "generic": "generic-ldap", "generic_ldap": "generic-ldap",
    "azuread": "graph", "entra": "graph", "entraid": "graph", "azure": "graph",
    "hybrid": "graph", "hybrid-ad": "graph",
    "okta": "okta", "google": "google", "gsuite": "google",
    "google-workspace": "google", "google_workspace": "google", "workspace": "google",
    "jumpcloud": "jumpcloud", "jc": "jumpcloud",
    "native": "native", "sqlserver": "native", "sql": "native", "none": "native",
    "ldap": "ldap", "graph": "graph", "generic-ldap": "generic-ldap",
}


def get_source(config: dict):
    """Build the configured identity source. Raises IntegrationError on bad config."""
    cfg = config or {}
    raw = (cfg.get("type") or "auto").lower()
    kind = _ALIASES.get(raw, raw if raw != "auto" else _auto_detect(cfg))
    hybrid = raw in ("hybrid", "hybrid-ad")
    if kind == "ldap":
        return LdapADSource(cfg)
    if kind == "generic-ldap":
        return LdapADSource(cfg, generic=True)
    if kind == "graph":
        return GraphADSource(cfg, hybrid=hybrid)
    if kind == "okta":
        return OktaADSource(cfg)
    if kind == "google":
        return GoogleWorkspaceADSource(cfg)
    if kind == "jumpcloud":
        return JumpCloudADSource(cfg)
    if kind == "native":
        return NativeSource(cfg)
    raise IntegrationError(
        f"Unknown access.ad.type '{raw}' (use ldap | generic-ldap | graph | hybrid | "
        f"okta | google | jumpcloud | native | auto).")


def _auto_detect(cfg) -> str:
    """Infer the identity source from the config shape."""
    if cfg.get("okta_domain") or cfg.get("okta_org_url"):
        return "okta"
    if cfg.get("delegated_admin") or cfg.get("workspace_domain") or cfg.get("customer"):
        return "google"
    if cfg.get("jumpcloud_api_key") or cfg.get("api_key") and cfg.get("jumpcloud"):
        return "jumpcloud"
    if cfg.get("tenant_id") or (cfg.get("client_secret") and cfg.get("client_id")):
        return "graph"
    domain = str(cfg.get("domain") or cfg.get("base_dn") or "")
    if "onmicrosoft.com" in domain.lower():
        return "graph"
    if cfg.get("server") or cfg.get("base_dn"):
        return "ldap"
    return "native"


def _looks_cloud(cfg) -> bool:   # kept for backward compat
    return _auto_detect(cfg) == "graph"


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


# Default attribute names: Active Directory, then generic (RFC2307 / OpenLDAP).
_AD_MAP = {"uid": "sAMAccountName", "mail": "mail", "display": "displayName",
           "upn": "userPrincipalName", "member": "memberOf", "title": "title",
           "dept": "department", "disabled": "userAccountControl", "object_class": "user"}
_GENERIC_MAP = {"uid": "uid", "mail": "mail", "display": "cn", "upn": "mail",
                "member": "memberOf", "title": "title", "dept": "departmentNumber",
                "disabled": "", "object_class": "person"}


class LdapADSource:
    """LDAP source for AD and generic (non-Microsoft) directories. Attribute
    names default to AD, or to RFC2307/OpenLDAP names when ``generic=True``, and
    can be overridden individually via ``access.ad.attributes``."""
    def __init__(self, cfg: dict, generic: bool = False):
        self.cfg = cfg or {}
        self.generic = generic
        self.source = "generic-ldap" if generic else "ldap"
        base = dict(_GENERIC_MAP if generic else _AD_MAP)
        base.update(self.cfg.get("attributes") or {})
        self.attr = base

    def _need(self):
        missing = [k for k in ("server", "base_dn") if not self.cfg.get(k)]
        if missing:
            raise IntegrationError(
                f"access.ad ({self.source}) is missing: {', '.join(missing)}. "
                f"Set server, base_dn (and bind_dn/bind_password) under access.ad.")

    def get_user(self, identifier: str) -> ADUser:
        self._need()
        conn = build_connection(self.cfg)
        ident = _ldap_escape(identifier)
        a = self.attr
        or_terms = f"({a['uid']}={ident})({a['mail']}={ident})"
        if a.get("upn") and a["upn"] not in (a["uid"], a["mail"]):
            or_terms += f"({a['upn']}={ident})"
        flt = f"(&(objectClass={a['object_class']})(|{or_terms}))"
        wanted = [v for v in (a["uid"], a["mail"], a["display"], a["upn"], a["member"],
                              a["title"], a["dept"], a["disabled"], "distinguishedName") if v]
        conn.search(self.cfg["base_dn"], flt, search_scope="SUBTREE", attributes=wanted)
        entries = list(getattr(conn, "entries", []) or [])
        if not entries:
            return ADUser(identifier=identifier, found=False, source=self.source)
        e = entries[0]
        member_of = _attr(e, a["member"], [])
        if isinstance(member_of, str):
            member_of = [member_of]
        groups = [_cn(dn) for dn in member_of if dn]
        sam = str(_attr(e, a["uid"]))
        disabled = False
        if a.get("disabled") == "userAccountControl":
            try:
                disabled = bool(int(_attr(e, "userAccountControl", 0)) & 0x2)
            except (TypeError, ValueError):
                disabled = False
        domain = self.cfg.get("netbios_domain") or self.cfg.get("domain", "")
        login = f"{domain}\\{sam}" if domain and sam else sam
        return ADUser(
            identifier=identifier, display_name=str(_attr(e, a["display"])) or sam,
            sam_account_name=sam, user_principal_name=str(_attr(e, a["upn"])),
            email=str(_attr(e, a["mail"])), distinguished_name=str(_attr(e, "distinguishedName")),
            login=login, title=str(_attr(e, a["title"])), department=str(_attr(e, a["dept"])),
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
    def __init__(self, cfg: dict, hybrid: bool = False):
        self.cfg = cfg or {}
        self.hybrid = hybrid
        self.source = "hybrid" if hybrid else "graph"

    def _need(self):
        missing = [k for k in ("tenant_id", "client_id", "client_secret") if not self.cfg.get(k)]
        if missing:
            raise IntegrationError(
                f"access.ad ({self.source}) is missing: {', '.join(missing)}. "
                f"Set tenant_id, client_id, client_secret under access.ad.")

    def get_user(self, identifier: str) -> ADUser:
        self._need()
        token = acquire_token(self.cfg)
        sel = ("displayName,userPrincipalName,mail,jobTitle,department,accountEnabled,"
               "onPremisesSamAccountName,onPremisesDomainName")
        u = graph_get(f"/users/{identifier}", token, params={"$select": sel})
        if u is None:
            return ADUser(identifier=identifier, found=False, source=self.source)
        groups = self._groups(token, identifier)
        sam = u.get("onPremisesSamAccountName") or (u.get("userPrincipalName", "").split("@")[0])
        # In a hybrid tenant, prefer the on-prem DOMAIN\sam login so it matches the
        # Windows-group logins synced into SQL Server; else use the UPN.
        if self.hybrid and u.get("onPremisesSamAccountName"):
            dom = (u.get("onPremisesDomainName") or "").split(".")[0].upper()
            login = f"{dom}\\{sam}" if dom else sam
        else:
            login = u.get("userPrincipalName") or sam
        return ADUser(
            identifier=identifier, display_name=u.get("displayName") or sam,
            sam_account_name=sam, user_principal_name=u.get("userPrincipalName", ""),
            email=u.get("mail") or u.get("userPrincipalName", ""),
            login=login, title=u.get("jobTitle") or "", department=u.get("department") or "",
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


# --- Okta Universal Directory ----------------------------------------------

def okta_request(method: str, path: str, cfg: dict, *, timeout: float = 30.0, **kwargs):
    import requests
    base = (cfg.get("okta_domain") or cfg.get("okta_org_url") or "").rstrip("/")
    if not base:
        raise IntegrationError("access.ad (okta) needs okta_domain (e.g. https://acme.okta.com).")
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"SSWS {cfg.get('api_token', '')}")
    headers.setdefault("Accept", "application/json")
    resp = requests.request(method, f"{base}{path}", headers=headers, timeout=timeout, **kwargs)
    if resp.status_code == 404:
        return None
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Okta {method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


class OktaADSource:
    source = "okta"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def get_user(self, identifier: str) -> ADUser:
        if not self.cfg.get("api_token"):
            raise IntegrationError("access.ad (okta) needs an api_token.")
        u = okta_request("GET", f"/api/v1/users/{identifier}", self.cfg)
        if u is None:
            return ADUser(identifier=identifier, found=False, source=self.source)
        p = u.get("profile", {})
        groups = []
        gdata = okta_request("GET", f"/api/v1/users/{u['id']}/groups", self.cfg) or []
        for g in gdata:
            name = (g.get("profile") or {}).get("name")
            if name:
                groups.append(name)
        login = p.get("login", "")
        sam = login.split("@")[0] if login else identifier
        return ADUser(
            identifier=identifier,
            display_name=(f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
                          or p.get("displayName") or sam),
            sam_account_name=sam, user_principal_name=login, email=p.get("email", ""),
            login=login or sam, title=p.get("title", ""), department=p.get("department", ""),
            enabled=(u.get("status") == "ACTIVE"), groups=groups, source=self.source, found=True)


# --- Google Workspace (Admin SDK Directory API) ----------------------------

_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/admin.directory.user.readonly",
                  "https://www.googleapis.com/auth/admin.directory.group.readonly"]


def build_directory_service(cfg: dict):
    require("googleapiclient", "google-workspace")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    if cfg.get("service_account_file"):
        creds = service_account.Credentials.from_service_account_file(
            cfg["service_account_file"], scopes=_GOOGLE_SCOPES)
    elif cfg.get("service_account_info"):
        creds = service_account.Credentials.from_service_account_info(
            cfg["service_account_info"], scopes=_GOOGLE_SCOPES)
    else:
        raise IntegrationError("access.ad (google) needs service_account_file or service_account_info.")
    if cfg.get("delegated_admin"):
        creds = creds.with_subject(cfg["delegated_admin"])
    return build("admin", "directory_v1", credentials=creds, cache_discovery=False)


class GoogleWorkspaceADSource:
    source = "google"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def get_user(self, identifier: str) -> ADUser:
        svc = build_directory_service(self.cfg)
        try:
            u = svc.users().get(userKey=identifier).execute()
        except Exception as e:
            if "404" in str(e) or "notFound" in str(e):
                return ADUser(identifier=identifier, found=False, source=self.source)
            raise
        if not u:
            return ADUser(identifier=identifier, found=False, source=self.source)
        gdata = svc.groups().list(userKey=identifier, maxResults=200).execute()
        groups = [g.get("name") or g.get("email") for g in (gdata.get("groups") or []) if g]
        orgs = u.get("organizations") or [{}]
        title = orgs[0].get("title", "") if orgs else ""
        dept = orgs[0].get("department", "") if orgs else ""
        email = u.get("primaryEmail", "")
        sam = email.split("@")[0] if email else identifier
        return ADUser(
            identifier=identifier, display_name=(u.get("name") or {}).get("fullName") or sam,
            sam_account_name=sam, user_principal_name=email, email=email, login=email or sam,
            title=title, department=dept, enabled=not u.get("suspended", False),
            groups=[g for g in groups if g], source=self.source, found=True)


# --- JumpCloud -------------------------------------------------------------

def jc_request(method: str, path: str, cfg: dict, *, timeout: float = 30.0, **kwargs):
    import requests
    headers = kwargs.pop("headers", {})
    headers.setdefault("x-api-key", cfg.get("api_key") or cfg.get("jumpcloud_api_key", ""))
    headers.setdefault("Accept", "application/json")
    headers.setdefault("Content-Type", "application/json")
    if cfg.get("org_id"):
        headers.setdefault("x-org-id", cfg["org_id"])
    resp = requests.request(method, f"https://console.jumpcloud.com{path}",
                            headers=headers, timeout=timeout, **kwargs)
    if resp.status_code == 404:
        return None
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"JumpCloud {method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


class JumpCloudADSource:
    source = "jumpcloud"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def get_user(self, identifier: str) -> ADUser:
        if not (self.cfg.get("api_key") or self.cfg.get("jumpcloud_api_key")):
            raise IntegrationError("access.ad (jumpcloud) needs an api_key.")
        data = jc_request("POST", "/api/search/systemusers", self.cfg,
                          json={"filter": {"or": [{"email": identifier}, {"username": identifier}]},
                                "fields": "email username displayname firstname lastname "
                                          "jobTitle department suspended"})
        results = (data or {}).get("results", []) if isinstance(data, dict) else (data or [])
        if not results:
            return ADUser(identifier=identifier, found=False, source=self.source)
        u = results[0]
        uid = u.get("_id") or u.get("id")
        groups = self._groups(uid)
        username = u.get("username", "")
        return ADUser(
            identifier=identifier,
            display_name=u.get("displayname")
                         or f"{u.get('firstname', '')} {u.get('lastname', '')}".strip() or username,
            sam_account_name=username, user_principal_name=u.get("email", ""),
            email=u.get("email", ""), login=username or identifier,
            title=u.get("jobTitle", ""), department=u.get("department", ""),
            enabled=not u.get("suspended", False), groups=groups, source=self.source, found=True)

    def _groups(self, uid) -> list:
        member = jc_request("GET", f"/api/v2/users/{uid}/memberof", self.cfg) or []
        ids = {m.get("id") or (m.get("to") or {}).get("id") for m in member if m}
        if not ids:
            return []
        allg = jc_request("GET", "/api/v2/usergroups", self.cfg) or []
        names = {g.get("id"): g.get("name") for g in allg}
        return [names[i] for i in ids if i in names and names[i]]


# --- Native (no directory) -------------------------------------------------

class NativeSource:
    """No directory at all — pure SQL Server native logins. Returns the user as
    found with no AD groups, so access commands match on SQL-native login names."""
    source = "native"

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or {}

    def get_user(self, identifier: str) -> ADUser:
        return ADUser(identifier=identifier, display_name=identifier,
                      sam_account_name=identifier, login=identifier, groups=[],
                      source=self.source, found=True)
