"""SSO: OIDC bearer verification + SAML assertion validation + HTTP gating."""
import base64
from datetime import datetime, timezone, timedelta

import pytest

from sqldoc import authn, api
from sqldoc.authn import (AuthConfig, AuthError, Principal, parse_auth_config,
                          verify_bearer, verify_saml_response, Authenticator)


# --- config parsing + presets -----------------------------------------------

def test_no_auth_section_disabled():
    assert parse_auth_config({}).enabled is False


def test_azure_ad_preset():
    ac = parse_auth_config({"auth": {"provider": "azure_ad", "tenant_id": "t123",
                                     "client_id": "app1"}})
    assert ac.enabled and ac.method == "oidc"
    assert "login.microsoftonline.com/t123/v2.0" in ac.issuer
    assert ac.jwks_uri.endswith("/discovery/v2.0/keys")
    assert ac.audience == "app1"


def test_okta_preset():
    ac = parse_auth_config({"auth": {"provider": "okta", "domain": "dev-1.okta.com",
                                     "client_id": "c"}})
    assert ac.issuer == "https://dev-1.okta.com/oauth2/default"
    assert ac.jwks_uri.endswith("/v1/keys")


def test_google_preset():
    ac = parse_auth_config({"auth": {"provider": "google", "client_id": "g"}})
    assert ac.issuer == "https://accounts.google.com"
    assert ac.audience == "g"


def test_azure_requires_tenant():
    with pytest.raises(ValueError):
        parse_auth_config({"auth": {"provider": "azure_ad", "client_id": "x"}})


def test_saml_requires_cert_and_audience():
    with pytest.raises(ValueError):
        parse_auth_config({"auth": {"provider": "saml", "sp_audience": "sp"}})
    with pytest.raises(ValueError):
        parse_auth_config({"auth": {"provider": "saml", "idp_cert": "CERT"}})


def test_generic_oidc_needs_issuer_and_jwks():
    with pytest.raises(ValueError):
        parse_auth_config({"auth": {"provider": "oidc", "audience": "a"}})


# --- OIDC bearer verification (injected decoder) ----------------------------

def _cfg(**kw):
    base = dict(enabled=True, method="oidc", provider="oidc",
                issuer="https://idp", audience="app", jwks_uri="https://idp/jwks")
    base.update(kw)
    return AuthConfig(**base)


def test_verify_bearer_returns_principal():
    claims = {"sub": "u1", "email": "a@corp.com", "name": "Ann", "groups": ["dba"]}
    p = verify_bearer("tok", _cfg(), decode_fn=lambda t, c: claims)
    assert p.subject == "u1" and p.email == "a@corp.com" and p.groups == ["dba"]


def test_verify_bearer_missing_token():
    with pytest.raises(AuthError):
        verify_bearer("", _cfg(), decode_fn=lambda t, c: {})


def test_verify_bearer_decode_error_wrapped():
    def boom(t, c):
        raise ValueError("bad signature")
    with pytest.raises(AuthError):
        verify_bearer("tok", _cfg(), decode_fn=boom)


def test_allowed_domains_enforced():
    claims = {"sub": "u", "email": "x@evil.com"}
    cfg = _cfg(allowed_domains=["corp.com"])
    with pytest.raises(AuthError):
        verify_bearer("t", cfg, decode_fn=lambda t, c: claims)
    # a corp.com email passes
    ok = verify_bearer("t", cfg, decode_fn=lambda t, c: {"sub": "u", "email": "y@corp.com"})
    assert ok.email == "y@corp.com"


def test_allowed_emails_and_groups():
    cfg = _cfg(allowed_emails=["boss@corp.com"], allowed_groups=["admins"])
    with pytest.raises(AuthError):
        verify_bearer("t", cfg, decode_fn=lambda t, c: {"sub": "u", "email": "boss@corp.com",
                                                         "groups": ["users"]})
    good = verify_bearer("t", cfg, decode_fn=lambda t, c: {"sub": "u", "email": "boss@corp.com",
                                                           "groups": ["admins"]})
    assert good.subject == "u"


def test_pyjwt_decode_missing_dep_message(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "jwt", None)
    with pytest.raises(AuthError) as e:
        authn._pyjwt_decode("tok", _cfg())
    assert "pip install sqldoc[sso]" in str(e.value)


# --- SAML assertion validation ----------------------------------------------

def _saml(name_id="alice@corp.com", audience="sp-entity", email=None,
          nb=None, na=None):
    nb = nb or (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    na = na or (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    attr = ""
    if email:
        attr = (f'<saml:AttributeStatement><saml:Attribute Name="email">'
                f'<saml:AttributeValue>{email}</saml:AttributeValue>'
                f'</saml:Attribute></saml:AttributeStatement>')
    return (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
        '<saml:Assertion>'
        f'<saml:Subject><saml:NameID>{name_id}</saml:NameID></saml:Subject>'
        f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{na}">'
        f'<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>'
        '</saml:AudienceRestriction></saml:Conditions>'
        f'{attr}'
        '</saml:Assertion></samlp:Response>')


def _saml_cfg(**kw):
    base = dict(enabled=True, method="saml", provider="saml",
                idp_cert="CERT", sp_audience="sp-entity")
    base.update(kw)
    return AuthConfig(**base)


def test_saml_valid_assertion():
    p = verify_saml_response(_saml(), _saml_cfg(), verify_signature=lambda x, c: None)
    assert p.subject == "alice@corp.com" and p.email == "alice@corp.com"
    assert p.method == "saml"


def test_saml_expired():
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(AuthError):
        verify_saml_response(_saml(na=past), _saml_cfg(), verify_signature=lambda x, c: None)


def test_saml_audience_mismatch():
    with pytest.raises(AuthError):
        verify_saml_response(_saml(audience="other"), _saml_cfg(),
                             verify_signature=lambda x, c: None)


def test_saml_signature_failure_propagates():
    def bad(x, c):
        raise AuthError("bad signature")
    with pytest.raises(AuthError):
        verify_saml_response(_saml(), _saml_cfg(), verify_signature=bad)


def test_saml_domain_allowlist():
    cfg = _saml_cfg(allowed_domains=["corp.com"])
    ok = verify_saml_response(_saml(name_id="bob@corp.com"), cfg,
                              verify_signature=lambda x, c: None)
    assert ok.email == "bob@corp.com"
    with pytest.raises(AuthError):
        verify_saml_response(_saml(name_id="mallory@evil.com"), cfg,
                             verify_signature=lambda x, c: None)


# --- Authenticator HTTP gate ------------------------------------------------

def test_authenticator_disabled_allows():
    a = Authenticator(AuthConfig(enabled=False))
    assert a.authenticate({}) == (True, None)


def test_authenticator_oidc_bearer():
    a = Authenticator(_cfg(), decode_fn=lambda t, c: {"sub": "u", "email": "a@corp.com"})
    ok, principal = a.authenticate({"Authorization": "Bearer xyz"})
    assert ok and principal.subject == "u"
    ok2, err = a.authenticate({})
    assert ok2 is False and "Bearer" in err


def test_authenticator_saml_header():
    a = Authenticator(_saml_cfg(), verify_signature=lambda x, c: None)
    b64 = base64.b64encode(_saml().encode()).decode()
    ok, principal = a.authenticate({"X-SAML-Response": b64})
    assert ok and principal.email == "alice@corp.com"
    ok2, err = a.authenticate({})
    assert ok2 is False


# --- REST API integration ---------------------------------------------------

def test_api_sso_gate(monkeypatch):
    from sqldoc.adapters.base import Capabilities
    from sqldoc.extractor import Table, Column

    class _A:
        dialect = "sqlserver"; display_name = "SQL Server"
        capabilities = Capabilities()
        def extract_metadata(self): return [Table("dbo", "T", 1, columns=[
            Column("Id", "int", 4, False, True, False, None, None)])]
        def extract_views(self): return []
        def extract_procedures(self): return []

    monkeypatch.setattr(api, "get_adapter", lambda cs, d=None: _A())
    a = Authenticator(_cfg(), decode_fn=lambda t, c: {"sub": "u", "email": "a@corp.com"})
    ctx = {"conn_str": "cs", "dialect": "sqlserver", "database": "DB", "authn": a}
    # no token -> 401
    status, _ = api.dispatch("GET", "/api/doc", {}, {}, ctx)
    assert status == 401
    # valid bearer -> 200
    status, payload = api.dispatch("GET", "/api/doc", {"Authorization": "Bearer t"}, {}, ctx)
    assert status == 200 and payload["database"] == "DB"


def test_dashboard_sso_gate(tmp_path):
    import threading
    import urllib.request
    import urllib.error
    from sqldoc.agent.store import AgentStore
    from sqldoc.agent.dashboard import make_server

    store = AgentStore(str(tmp_path / "agent.db"))
    store.add_event("prod", "schema_change", "x")
    a = Authenticator(_cfg(), decode_fn=lambda t, c: {"sub": "u", "email": "a@corp.com"})
    server = make_server(store, 0, authn=a)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # no token -> 401
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
        # valid bearer -> 200
        req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                     headers={"Authorization": "Bearer t"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_api_key_or_sso(monkeypatch):
    from sqldoc.adapters.base import Capabilities
    from sqldoc.extractor import Table

    class _A:
        dialect = "sqlserver"; display_name = "SQL Server"
        capabilities = Capabilities()
        def extract_metadata(self): return [Table("dbo", "T", 1, columns=[])]
        def extract_views(self): return []
        def extract_procedures(self): return []

    monkeypatch.setattr(api, "get_adapter", lambda cs, d=None: _A())
    a = Authenticator(_cfg(), decode_fn=lambda t, c: (_ for _ in ()).throw(ValueError("bad")))
    ctx = {"conn_str": "cs", "dialect": "sqlserver", "database": "DB",
           "api_key": "secret", "authn": a}
    # api key satisfies even though SSO would fail
    status, _ = api.dispatch("GET", "/api/doc", {"X-API-Key": "secret"}, {}, ctx)
    assert status == 200
    # neither -> 401
    status, _ = api.dispatch("GET", "/api/doc", {}, {}, ctx)
    assert status == 401
