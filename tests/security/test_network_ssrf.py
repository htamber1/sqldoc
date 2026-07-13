"""Security: outbound HTTP enforces timeouts, TLS, and SSRF-safe redirects."""
import pytest

from sqldoc import nethttp
from sqldoc.validation import (ValidationError, assert_safe_outbound_url,
                               validate_url, validate_port, is_internal_host)


class FakeResp:
    def __init__(self, status=200, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}


def test_url_scheme_allowlist():
    assert validate_url("https://ok.example/x")
    for bad in ("file:///etc/passwd", "gopher://x", "ftp://x/y"):
        with pytest.raises(ValidationError):
            validate_url(bad)


def test_port_range():
    assert validate_port(8090) == 8090
    for bad in (0, 70000, -1, "abc"):
        with pytest.raises(ValidationError):
            validate_port(bad)


def test_metadata_and_internal_detection():
    assert is_internal_host("127.0.0.1")
    assert is_internal_host("169.254.169.254")
    assert is_internal_host("10.0.0.5")
    assert not is_internal_host("142.250.72.4")
    for bad in ("http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1/admin", "http://[::1]/"):
        with pytest.raises(ValidationError):
            assert_safe_outbound_url(bad)


def test_safe_request_applies_default_timeout(monkeypatch):
    seen = {}
    def fake_request(method, url, **kw):
        seen.update(kw); seen["url"] = url
        return FakeResp(200)
    monkeypatch.setattr("requests.request", fake_request)
    nethttp.safe_request("GET", "https://ok.example/x")
    assert seen["timeout"] == nethttp.DEFAULT_TIMEOUT
    assert seen["allow_redirects"] is False   # redirects handled manually


def test_safe_request_rejects_verify_false():
    with pytest.raises(ValidationError):
        nethttp.safe_request("GET", "https://ok.example", verify=False)


def test_safe_request_blocks_metadata_preflight():
    with pytest.raises(ValidationError):
        nethttp.safe_request("GET", "http://169.254.169.254/latest/")


def test_safe_request_blocks_redirect_to_internal(monkeypatch):
    # External origin 302s to an internal address -> refused (SSRF pivot).
    def fake_request(method, url, **kw):
        return FakeResp(302, location="http://127.0.0.1/secrets")
    monkeypatch.setattr("requests.request", fake_request)
    with pytest.raises(ValidationError):
        nethttp.safe_request("GET", "https://evil.example/redir")


def test_safe_request_blocks_redirect_to_metadata(monkeypatch):
    def fake_request(method, url, **kw):
        return FakeResp(302, location="http://169.254.169.254/latest/meta-data/")
    monkeypatch.setattr("requests.request", fake_request)
    with pytest.raises(ValidationError):
        nethttp.safe_request("GET", "https://evil.example/redir")
