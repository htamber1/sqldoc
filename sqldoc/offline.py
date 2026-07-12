"""Air-gap / offline verification for generated HTML reports.

Every sqldoc HTML report is meant to be **fully self-contained** — all CSS and
JavaScript inlined, no CDN scripts, web fonts, or remote images — so it renders
identically on an isolated, air-gapped network. This module scans rendered HTML
for any reference that a browser would fetch off-box and reports it, powering the
``--verify-offline`` flag.

What counts as "external" (would break air-gap):
* ``<script src>``, ``<link href>`` (stylesheets), ``<img src>``, ``<use
  xlink:href>``, ``<iframe/video/audio/source/object/embed>`` pointing at an
  ``http(s)://`` or protocol-relative ``//host`` URL,
* CSS ``@import`` and ``url(...)`` pointing off-box.

Explicitly NOT flagged: ``data:`` URIs (inlined), in-page ``#anchor`` links,
``mailto:``/``tel:``, and XML namespaces (``xmlns="http://www.w3.org/..."`` —
declarations, never fetched). A plain ``<a href="http…">`` hyperlink is reported
separately as a *link* (informational) — it does not auto-load, so it does not
break offline rendering.
"""
import re
from dataclasses import dataclass

# URL values that are safe (inlined or non-network) and never flagged.
_SAFE_PREFIX = re.compile(r'^\s*(data:|#|mailto:|tel:|javascript:|about:)', re.I)


@dataclass
class ExternalRef:
    kind: str        # css-import / css-url / src / data / poster / srcset / xlink:href / link-href / a-link
    url: str

    @property
    def is_blocking(self) -> bool:
        """True if this reference would fail to load on an air-gapped network
        (i.e. an auto-fetched resource, not a plain hyperlink)."""
        return self.kind != "a-link"


def _is_external(val: str) -> bool:
    v = (val or "").strip()
    if not v or _SAFE_PREFIX.match(v):
        return False
    if v.startswith(("http://", "https://")):
        return True
    if v.startswith("//"):        # protocol-relative -> fetched from the network
        return True
    return False                  # relative path / anchor -> local


def find_external_refs(html: str) -> list:
    """Return every external resource/link reference in `html`."""
    refs = []
    seen = set()

    def add(kind, url):
        # Dedup by URL: an @import url(...) matches both the @import and the
        # url() pattern, but it is one external resource.
        if url and url not in seen:
            seen.add(url)
            refs.append(ExternalRef(kind, url))

    # CSS @import ... and url(...) pointing off-box.
    for m in re.finditer(r'@import\s+(?:url\(\s*)?["\']?((?:https?:)?//[^"\')\s]+)', html, re.I):
        add("css-import", m.group(1))
    for m in re.finditer(r'url\(\s*["\']?((?:https?:)?//[^"\')\s]+)', html, re.I):
        add("css-url", m.group(1))

    # Resource-loading attributes other than href.
    for m in re.finditer(r'\b(src|data|xlink:href|poster|srcset)\s*=\s*["\']([^"\']+)["\']', html, re.I):
        if _is_external(m.group(2)):
            add(m.group(1).lower(), m.group(2).strip())

    # href — separate a real <a> hyperlink (informational) from a resource
    # <link>/<base>/<area> href.
    for m in re.finditer(r'<(\w+)\b[^>]*?\bhref\s*=\s*["\']([^"\']+)["\']', html, re.I | re.S):
        tag, val = m.group(1).lower(), m.group(2).strip()
        if _is_external(val):
            add("a-link" if tag == "a" else f"{tag}-href", val)

    return refs


def verify_file(path: str) -> list:
    """Scan an HTML file on disk for external references."""
    with open(path, encoding="utf-8") as f:
        return find_external_refs(f.read())


def blocking_refs(refs) -> list:
    return [r for r in refs if r.is_blocking]
