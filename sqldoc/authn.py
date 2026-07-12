"""Single sign-on for the agent dashboard and the REST API.

Two mechanisms, configured under the ``auth:`` section of ``.sqldoc.yml``:

* **OAuth2 / OIDC** (``method: oidc``) — callers present an
  ``Authorization: Bearer <JWT>`` access/ID token. The token's RS256 signature is
  verified against the identity provider's JWKS, along with its issuer, audience,
  and expiry, and then an optional email/domain/group allowlist is enforced.
  Presets for **Azure AD**, **Okta**, and **Google Workspace** fill in the
  issuer + JWKS URL from a tenant/domain so only the client id is required.

* **SAML 2.0** (``method: saml``) — a SAML ``Response`` (base64) is validated:
  its assertion ``Conditions`` (NotBefore / NotOnOrAfter), ``AudienceRestriction``,
  and XML signature are checked, and the ``NameID`` + attributes become the
  principal. Signature verification is delegated to ``signxml`` (optional dep).

Both are dependency-light: the JWT/JWKS verification uses **PyJWT** and SAML
signature checking uses **signxml**, each installed on demand
(``pip install sqldoc[sso]`` / ``sqldoc[saml]``). Every verification entry point
accepts an injectable verifier so the logic is unit-testable without the crypto
libraries or a live IdP.
"""
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone


class AuthError(Exception):
    """Raised when authentication fails (bad/expired token, not allowlisted, ...)."""


@dataclass
class Principal:
    subject: str
    email: str = None
    name: str = None
    groups: list = field(default_factory=list)
    method: str = "oidc"


@dataclass
class AuthConfig:
    enabled: bool = False
    method: str = "oidc"           # oidc | saml
    provider: str = "oidc"         # azure_ad | okta | google | oidc | saml
    # OIDC
    issuer: str = None
    audience: str = None
    jwks_uri: str = None
    # allowlists (applied after signature/claim validation)
    allowed_domains: list = field(default_factory=list)
    allowed_emails: list = field(default_factory=list)
    allowed_groups: list = field(default_factory=list)
    # SAML
    idp_cert: str = None           # IdP signing certificate (PEM)
    sp_audience: str = None        # expected SAML AudienceRestriction (SP entity id)


# --- provider presets -------------------------------------------------------

def _apply_preset(provider, raw, cfg):
    """Fill issuer/jwks_uri/audience from a provider preset + its parameters."""
    if provider == "azure_ad":
        tenant = raw.get("tenant_id") or raw.get("tenant")
        if not tenant:
            raise ValueError("auth provider 'azure_ad' needs a 'tenant_id'.")
        cfg.issuer = cfg.issuer or f"https://login.microsoftonline.com/{tenant}/v2.0"
        cfg.jwks_uri = cfg.jwks_uri or f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
        cfg.audience = cfg.audience or raw.get("client_id")
    elif provider == "okta":
        domain = raw.get("domain")
        if not domain:
            raise ValueError("auth provider 'okta' needs a 'domain' (e.g. dev-123.okta.com).")
        auth_server = raw.get("auth_server", "default")
        base = f"https://{domain}/oauth2/{auth_server}"
        cfg.issuer = cfg.issuer or base
        cfg.jwks_uri = cfg.jwks_uri or f"{base}/v1/keys"
        cfg.audience = cfg.audience or raw.get("audience") or raw.get("client_id") or "api://default"
    elif provider == "google":
        cfg.issuer = cfg.issuer or "https://accounts.google.com"
        cfg.jwks_uri = cfg.jwks_uri or "https://www.googleapis.com/oauth2/v3/certs"
        cfg.audience = cfg.audience or raw.get("client_id")
    # generic "oidc" / "saml": nothing to preset (explicit values required)


def parse_auth_config(cfg: dict) -> AuthConfig:
    """Build an AuthConfig from a loaded .sqldoc.yml mapping's ``auth:`` section.
    Returns a disabled config when no ``auth:`` section is present."""
    auth = (cfg or {}).get("auth")
    if not auth:
        return AuthConfig(enabled=False)
    if not isinstance(auth, dict):
        raise ValueError("The 'auth:' config must be a mapping.")
    if auth.get("enabled") is False:
        return AuthConfig(enabled=False)

    provider = (auth.get("provider") or "oidc").lower()
    method = (auth.get("method") or ("saml" if provider == "saml" else "oidc")).lower()
    if method not in ("oidc", "saml"):
        raise ValueError("auth.method must be 'oidc' or 'saml'.")

    ac = AuthConfig(
        enabled=True, method=method, provider=provider,
        issuer=auth.get("issuer"), audience=auth.get("audience"),
        jwks_uri=auth.get("jwks_uri"),
        allowed_domains=[d.lower() for d in (auth.get("allowed_domains") or [])],
        allowed_emails=[e.lower() for e in (auth.get("allowed_emails") or [])],
        allowed_groups=list(auth.get("allowed_groups") or []),
        idp_cert=auth.get("idp_cert"),
        sp_audience=auth.get("sp_audience") or auth.get("audience"),
    )

    if method == "oidc":
        _apply_preset(provider, auth, ac)
        if not ac.issuer or not ac.jwks_uri:
            raise ValueError(
                "OIDC auth needs an 'issuer' + 'jwks_uri' (or a provider preset: "
                "azure_ad/okta/google with its tenant/domain).")
    else:  # saml
        if not ac.idp_cert:
            raise ValueError("SAML auth needs the IdP signing certificate ('idp_cert').")
        if not ac.sp_audience:
            raise ValueError("SAML auth needs an 'sp_audience' (the SP entity id).")
    return ac


# --- allowlist enforcement --------------------------------------------------

def _check_allowed(principal: Principal, cfg: AuthConfig):
    email = (principal.email or "").lower()
    if cfg.allowed_emails and email not in cfg.allowed_emails:
        raise AuthError(f"'{principal.email}' is not on the allowed-emails list.")
    if cfg.allowed_domains:
        domain = email.split("@")[-1] if "@" in email else ""
        if domain not in cfg.allowed_domains:
            raise AuthError(f"email domain '{domain}' is not on the allowed-domains list.")
    if cfg.allowed_groups:
        if not set(principal.groups or []) & set(cfg.allowed_groups):
            raise AuthError("principal is not a member of any allowed group.")


# --- OIDC / OAuth2 bearer ---------------------------------------------------

def _pyjwt_decode(token, cfg):
    """Verify an RS256 JWT against the IdP JWKS + issuer/audience/exp (PyJWT)."""
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError:
        raise AuthError(
            "OIDC verification needs PyJWT with crypto. Install it with: "
            "pip install sqldoc[sso]")
    signing_key = PyJWKClient(cfg.jwks_uri).get_signing_key_from_jwt(token).key
    return jwt.decode(
        token, signing_key, algorithms=["RS256"],
        audience=cfg.audience, issuer=cfg.issuer,
        options={"require": ["exp"], "verify_aud": bool(cfg.audience)})


def _principal_from_claims(claims, cfg):
    groups = claims.get("groups") or claims.get("roles") or []
    if isinstance(groups, str):
        groups = [groups]
    return Principal(
        subject=claims.get("sub") or claims.get("oid") or "",
        email=(claims.get("email") or claims.get("preferred_username")
               or claims.get("upn")),
        name=claims.get("name"),
        groups=list(groups),
        method="oidc")


def verify_bearer(token: str, cfg: AuthConfig, decode_fn=None) -> Principal:
    """Verify an OIDC bearer token and return the Principal, or raise AuthError.
    `decode_fn(token, cfg) -> claims` is injectable for testing; it must return
    the claims of a signature+expiry-verified token."""
    if not token:
        raise AuthError("missing bearer token.")
    try:
        claims = (decode_fn or _pyjwt_decode)(token, cfg)
    except AuthError:
        raise
    except Exception as e:
        raise AuthError(f"token verification failed: {type(e).__name__}: {e}")
    principal = _principal_from_claims(claims, cfg)
    _check_allowed(principal, cfg)
    return principal


# --- SAML 2.0 ---------------------------------------------------------------

_SAML_NS = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion"}


def _parse_dt(value):
    # SAML uses xsd:dateTime, e.g. 2026-07-12T08:00:00Z
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)


def _signxml_verify(xml_bytes, cert_pem):
    try:
        from signxml import XMLVerifier
    except ImportError:
        raise AuthError(
            "SAML signature verification needs signxml. Install it with: "
            "pip install sqldoc[saml]")
    from defusedxml.ElementTree import fromstring
    XMLVerifier().verify(fromstring(xml_bytes), x509_cert=cert_pem)


def verify_saml_response(xml, cfg: AuthConfig, verify_signature=None, now=None) -> Principal:
    """Validate a decoded SAML Response XML and return the Principal, or raise
    AuthError. Checks the assertion Conditions (NotBefore/NotOnOrAfter),
    AudienceRestriction, and XML signature (delegated to `verify_signature(xml,
    cert)` — defaults to signxml), then extracts the NameID + attributes."""
    from defusedxml.ElementTree import fromstring
    if isinstance(xml, str):
        xml_bytes = xml.encode("utf-8")
    else:
        xml_bytes = xml
    try:
        root = fromstring(xml_bytes)
    except Exception as e:
        raise AuthError(f"malformed SAML XML: {e}")

    assertion = root.find(".//saml:Assertion", _SAML_NS)
    if assertion is None:
        raise AuthError("SAML Response has no Assertion.")

    now = now or datetime.now(timezone.utc)
    conditions = assertion.find("saml:Conditions", _SAML_NS)
    if conditions is not None:
        nb = conditions.get("NotBefore")
        na = conditions.get("NotOnOrAfter")
        if nb and now < _parse_dt(nb):
            raise AuthError("SAML assertion is not yet valid (NotBefore).")
        if na and now >= _parse_dt(na):
            raise AuthError("SAML assertion has expired (NotOnOrAfter).")
        audiences = [a.text for a in conditions.findall(".//saml:Audience", _SAML_NS)]
        if audiences and cfg.sp_audience not in audiences:
            raise AuthError(f"SAML audience mismatch (expected '{cfg.sp_audience}').")

    # Signature (delegated; default requires signxml).
    (verify_signature or _signxml_verify)(xml_bytes, cfg.idp_cert)

    name_id_el = assertion.find(".//saml:Subject/saml:NameID", _SAML_NS)
    name_id = name_id_el.text if name_id_el is not None else None
    if not name_id:
        raise AuthError("SAML assertion has no NameID.")

    attrs = {}
    for a in assertion.findall(".//saml:AttributeStatement/saml:Attribute", _SAML_NS):
        key = a.get("Name") or ""
        vals = [v.text for v in a.findall("saml:AttributeValue", _SAML_NS)]
        attrs[key.lower()] = vals

    def _first(*names):
        for n in names:
            if attrs.get(n):
                return attrs[n][0]
        return None

    email = _first("email", "mail",
                   "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress") or \
        (name_id if "@" in name_id else None)
    groups = attrs.get("groups") or attrs.get("roles") or []

    principal = Principal(
        subject=name_id, email=email,
        name=_first("name", "displayname",
                    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"),
        groups=list(groups), method="saml")
    _check_allowed(principal, cfg)
    return principal


# --- HTTP integration -------------------------------------------------------

class Authenticator:
    """Wraps an AuthConfig and authenticates an inbound request. Used by both the
    REST API and the agent dashboard as an auth gate."""

    def __init__(self, cfg: AuthConfig, decode_fn=None, verify_signature=None):
        self.cfg = cfg
        self._decode_fn = decode_fn
        self._verify_signature = verify_signature

    @property
    def enabled(self):
        return bool(self.cfg and self.cfg.enabled)

    def bearer_from_headers(self, headers):
        raw = headers.get("Authorization") or headers.get("authorization")
        if not raw:
            return None
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return None

    def authenticate(self, headers):
        """Authenticate a request. Returns (True, Principal) or (False, error_str)."""
        if not self.enabled:
            return True, None
        if self.cfg.method == "oidc":
            token = self.bearer_from_headers(headers)
            if not token:
                return False, "missing 'Authorization: Bearer <token>' header"
            try:
                return True, verify_bearer(token, self.cfg, decode_fn=self._decode_fn)
            except AuthError as e:
                return False, str(e)
        # SAML: a validated assertion is carried in an X-SAML-Response header
        # (base64), typically set by the ACS / a proxy after the browser POST.
        raw = headers.get("X-SAML-Response") or headers.get("x-saml-response")
        if not raw:
            return False, "missing 'X-SAML-Response' header"
        try:
            xml = base64.b64decode(raw)
            principal = verify_saml_response(
                xml, self.cfg, verify_signature=self._verify_signature)
            return True, principal
        except AuthError as e:
            return False, str(e)
        except Exception as e:
            return False, f"SAML validation failed: {type(e).__name__}: {e}"


def build_authenticator(cfg: dict):
    """Return an Authenticator from a loaded config, or None if auth is off."""
    ac = parse_auth_config(cfg)
    return Authenticator(ac) if ac.enabled else None
