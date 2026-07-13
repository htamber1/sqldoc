"""SSRF-aware outbound HTTP helper.

All of sqldoc's outbound calls already (a) verify TLS certificates — we never
pass ``verify=False`` — and (b) pass an explicit ``timeout``. The remaining
network risk is **redirect-based SSRF**: ``requests`` follows redirects by
default, so a caller-influenced endpoint could 302 to an internal address
(``127.0.0.1``, a private range) or a cloud-metadata service
(``169.254.169.254``) and exfiltrate credentials or reach the control plane.

``safe_request`` follows redirects manually and vets every hop:

  * a **cloud-metadata** host is refused on *any* hop (never legitimate);
  * a redirect that crosses from an **external** origin to an **internal**
    address is refused (the classic SSRF pivot) even when internal direct
    connections are allowed — because a self-hosted integration (internal
    GitLab/Jira, or a localhost Ollama) is a legitimate *direct* target but
    should never be reached via an external server's redirect.

``allow_internal`` (default True) governs the *initial* host only, so configured
internal integrations keep working; set it False for a fully-untrusted URL.
"""
from __future__ import annotations

from urllib.parse import urlparse

from sqldoc.validation import ValidationError, is_internal_host, _METADATA_HOSTS

DEFAULT_TIMEOUT = 15  # seconds — applied when a caller doesn't pass one.
_MAX_REDIRECTS = 5


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _check_hop(url: str, origin_internal: bool, allow_internal: bool,
               is_redirect: bool) -> None:
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValidationError(f"refusing non-HTTP(S) URL scheme {scheme!r}.")
    host = _host(url)
    if not host:
        raise ValidationError("refusing URL with no host.")
    if host in _METADATA_HOSTS:
        raise ValidationError(
            f"refusing request to cloud-metadata host {host!r} (SSRF).")
    internal = is_internal_host(host)
    if is_redirect:
        # A redirect into an internal address from an external origin is the
        # SSRF pivot — always refuse it.
        if internal and not origin_internal:
            raise ValidationError(
                f"refusing redirect to internal address {host!r} (SSRF).")
    else:
        if internal and not allow_internal:
            raise ValidationError(
                f"refusing request to internal address {host!r} "
                "(allow_internal=False).")


def safe_request(method: str, url: str, *, allow_internal: bool = True,
                 timeout=None, session=None, **kwargs):
    """Perform an outbound HTTP request with SSRF-safe manual redirect handling.

    Mirrors ``requests.request`` but disables its auto-redirect and re-issues
    each hop after validating the target. TLS verification is left at the
    ``requests`` default (on); ``verify=False`` is not accepted.
    """
    import requests

    if kwargs.get("verify") is False:
        raise ValidationError("TLS verification cannot be disabled.")
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    kwargs.pop("allow_redirects", None)   # we handle redirects ourselves
    req = session or requests

    origin_internal = is_internal_host(_host(url))
    _check_hop(url, origin_internal, allow_internal, is_redirect=False)

    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        resp = req.request(method, current, timeout=timeout,
                           allow_redirects=False, **kwargs)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            nxt = requests.compat.urljoin(current, resp.headers["Location"])
            _check_hop(nxt, origin_internal, allow_internal, is_redirect=True)
            # 303 (and 301/302 in practice) turn the method into GET with no body.
            if resp.status_code == 303:
                method, kwargs = "GET", {k: v for k, v in kwargs.items()
                                         if k not in ("data", "json", "files")}
            current = nxt
            continue
        return resp
    raise ValidationError(f"too many redirects (> {_MAX_REDIRECTS}).")
