"""Regression tests for the outbound-transport SSRF divide (v3.2.0, 17-18).

Found while building the containment for real notification-channel testing: the
channels were to be pointed at a loopback sink, which made the transport's
redirect handling load-bearing in a direction it had never been examined in.

PATCH 17 -- `nethttp._check_hop` vetted only ONE direction of the divide.
    `safe_request` refused an external -> internal redirect (the classic SSRF
    pivot) but happily followed internal -> external. That is the exfiltration
    direction: `safe_request` re-issues every hop with the caller's ORIGINAL
    kwargs, and an explicit `headers={"Authorization": ...}` survives that
    manual re-issue -- unlike the `auth=` parameter, which requests strips on a
    cross-host redirect. So any self-hosted or attacker-influenced internal
    endpoint (an internal GitLab/Jira, a localhost Ollama, a self-hosted webhook
    receiver) could 302 an authenticated sqldoc POST -- bearer token, routing
    key, webhook secret and body -- to any external host it chose.

    A redirect may now not cross the internal/external divide in EITHER
    direction. Same-side redirects (internal->internal, external->external) are
    unaffected, so ordinary webhook and integration redirects keep working.

PATCH 18 -- `servicenow.sn_request` bypassed the SSRF-aware transport entirely.
    Every other config-URL channel (generic webhook, Slack, Teams) routes
    through `safe_request`. ServiceNow -- whose `instance_url` is operator
    config exactly like the others, and which attaches BASIC AUTH to every
    request -- called `requests.request` directly. It therefore followed
    redirects with the requests default auto-redirect and no hop vetting at
    all, including to cloud-metadata addresses.

Both patches are independent (different files); apply in either order.

Run with:  pytest test_transport_ssrf.py
"""
import pytest

from sqldoc import nethttp
from sqldoc.integrations import servicenow
from sqldoc.validation import ValidationError
from sqldoc.integrations.base import IntegrationError


# --- fakes -----------------------------------------------------------------

class FakeResp:
    def __init__(self, status=200, location=None,
                 body=b'{"result": {"number": "INC1"}}'):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.content = body
        self.text = body.decode() if isinstance(body, bytes) else str(body)

    def json(self):
        import json
        return json.loads(self.content)


class RecordingSession:
    """Records every hop `safe_request` actually issues."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0) if self.responses else FakeResp()

    @property
    def urls(self):
        return [u for _, u, _ in self.calls]


# --- patch 17: the redirect divide, both directions ----------------------

def test_internal_to_external_redirect_is_refused():
    """THE PATCH-17 BUG. A loopback origin 302-ing out to a real service was
    followed. It must now raise."""
    s = RecordingSession(FakeResp(302, location="https://hooks.slack.com/services/T/B/x"))
    with pytest.raises(ValidationError, match="internal -> external"):
        nethttp.safe_request("POST", "http://127.0.0.1:8081/slack",
                             json={"text": "hi"}, session=s)
    # And, crucially, the outbound hop was never issued.
    assert s.urls == ["http://127.0.0.1:8081/slack"]
    assert "hooks.slack.com" not in " ".join(s.urls)


def test_credentials_are_not_carried_across_the_divide():
    """The concrete harm: an Authorization header must never reach the external
    redirect target. Assert on what was SENT, not just on the exception."""
    s = RecordingSession(FakeResp(302, location="https://attacker.example.com/collect"))
    with pytest.raises(ValidationError):
        nethttp.safe_request("POST", "http://10.0.0.5/internal-hook",
                             headers={"Authorization": "Bearer SECRET-TOKEN"},
                             json={"x": 1}, session=s)
    assert len(s.calls) == 1
    assert s.urls == ["http://10.0.0.5/internal-hook"]
    for _, url, kw in s.calls:
        if "attacker.example.com" in url:
            raise AssertionError("credentials were sent to the redirect target")


def test_external_to_internal_redirect_still_refused():
    """Regression: the direction that was already covered must stay covered."""
    s = RecordingSession(FakeResp(302, location="http://169.254.1.1/"))
    with pytest.raises(ValidationError, match="external -> internal"):
        nethttp.safe_request("GET", "https://example.com/start", session=s)
    assert len(s.calls) == 1


def test_internal_to_internal_redirect_is_allowed():
    """Same-side redirects must keep working -- a self-hosted integration that
    redirects within its own network is legitimate."""
    s = RecordingSession(FakeResp(302, location="http://127.0.0.1:8081/moved"),
                         FakeResp(200))
    resp = nethttp.safe_request("POST", "http://127.0.0.1:8081/slack", session=s)
    assert resp.status_code == 200
    assert s.urls == ["http://127.0.0.1:8081/slack", "http://127.0.0.1:8081/moved"]


def test_external_to_external_redirect_is_allowed():
    s = RecordingSession(FakeResp(301, location="https://hooks.slack.com/new"),
                         FakeResp(200))
    resp = nethttp.safe_request("POST", "https://hooks.slack.com/old", session=s)
    assert resp.status_code == 200
    assert len(s.calls) == 2


def test_metadata_host_refused_on_a_redirect_hop():
    """Metadata is refused on any hop regardless of direction."""
    s = RecordingSession(FakeResp(302, location="http://169.254.169.254/latest/meta-data/"))
    with pytest.raises(ValidationError, match="metadata"):
        nethttp.safe_request("GET", "https://example.com/", session=s)


def test_non_redirect_request_is_unaffected():
    s = RecordingSession(FakeResp(200))
    resp = nethttp.safe_request("POST", "http://127.0.0.1:8081/slack", session=s)
    assert resp.status_code == 200
    assert len(s.calls) == 1


# --- patch 18: ServiceNow must use the SSRF-aware transport ----------------

SN_CFG = {"instance_url": "https://example.service-now.com",
          "username": "svc", "password": "pw"}


def test_servicenow_routes_through_safe_request(monkeypatch):
    """THE PATCH-18 BUG. sn_request must go through safe_request, not
    requests.request."""
    seen = {}

    def fake_safe_request(method, url, **kwargs):
        seen["called"] = True
        seen["url"] = url
        return FakeResp(200)

    monkeypatch.setattr(nethttp, "safe_request", fake_safe_request)
    servicenow.sn_request("GET", "/api/now/table/incident", SN_CFG)
    assert seen.get("called"), "sn_request bypassed safe_request"
    assert seen["url"] == "https://example.service-now.com/api/now/table/incident"


def test_servicenow_redirect_off_instance_is_refused(monkeypatch):
    """An instance_url that 302s to an internal address must be refused --
    with basic-auth credentials attached, this is the ServiceNow-shaped
    version of the same exfiltration."""
    s = RecordingSession(FakeResp(302, location="http://169.254.169.254/"))
    real = nethttp.safe_request

    def fake_safe_request(method, url, **kwargs):
        kwargs.pop("session", None)
        return real(method, url, session=s, **kwargs)

    monkeypatch.setattr(nethttp, "safe_request", fake_safe_request)
    with pytest.raises(IntegrationError, match="refused"):
        servicenow.sn_request("GET", "/api/now/table/incident", SN_CFG)
    assert len(s.calls) == 1


def test_servicenow_validation_error_becomes_integration_error(monkeypatch):
    """Callers catch IntegrationError; a raw ValidationError would escape the
    connector's error contract (create_issues catches IntegrationError only)."""
    def boom(method, url, **kwargs):
        raise ValidationError("refusing non-HTTP(S) URL scheme 'file'.")

    monkeypatch.setattr(nethttp, "safe_request", boom)
    with pytest.raises(IntegrationError) as ei:
        servicenow.sn_request("GET", "/api/now/table/incident", SN_CFG)
    assert "ServiceNow GET" in str(ei.value)


def test_servicenow_loopback_instance_is_allowed(monkeypatch):
    """A loopback instance_url is a legitimate DIRECT target (allow_internal
    defaults True) -- this is what lets the sink be used at all."""
    s = RecordingSession(FakeResp(200))
    real = nethttp.safe_request

    def fake_safe_request(method, url, **kwargs):
        kwargs.pop("session", None)
        return real(method, url, session=s, **kwargs)

    monkeypatch.setattr(nethttp, "safe_request", fake_safe_request)
    cfg = dict(SN_CFG, instance_url="http://127.0.0.1:8081")
    out = servicenow.sn_request("GET", "/api/now/table/incident", cfg)
    assert out == {"result": {"number": "INC1"}}
    assert s.urls == ["http://127.0.0.1:8081/api/now/table/incident"]


def test_servicenow_non_2xx_still_raises(monkeypatch):
    """Regression: the existing status check must survive the rewrite."""
    monkeypatch.setattr(nethttp, "safe_request",
                        lambda m, u, **kw: FakeResp(403, body=b"forbidden"))
    with pytest.raises(IntegrationError, match="403"):
        servicenow.sn_request("GET", "/api/now/table/incident", SN_CFG)


def test_servicenow_basic_auth_is_still_attached(monkeypatch):
    """The rewrite must not drop the credentials the connector needs."""
    seen = {}

    def fake_safe_request(method, url, **kwargs):
        seen.update(kwargs)
        return FakeResp(200)

    monkeypatch.setattr(nethttp, "safe_request", fake_safe_request)
    servicenow.sn_request("GET", "/api/now/table/incident", SN_CFG)
    assert seen["auth"] == ("svc", "pw")
    assert seen["headers"]["Accept"] == "application/json"
