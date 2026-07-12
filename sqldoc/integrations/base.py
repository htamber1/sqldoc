"""Shared plumbing for the sqldoc integration suite.

Every integration (SharePoint, Confluence, Notion, Google Drive, Box, Jira,
ServiceNow, Azure DevOps, Power BI, generic webhook, and the alerting channels)
follows the same shape:

* the third-party SDK (or nothing but ``requests``) is an **optional** dependency
  imported lazily via :func:`require`, so ``import sqldoc`` never pulls it in and
  a missing extra produces a clear ``pip install sqldoc[...]`` message;
* the actual network call goes through a small, **module-level** transport
  function so tests can monkeypatch it without touching a real service (the same
  pattern the agent's ``notify`` module already uses);
* ``test()`` verifies connectivity/auth and ``push(...)`` does the work, each
  returning a plain result dict that the CLI renders.
"""
from dataclasses import dataclass, field


class IntegrationError(Exception):
    """A user-facing integration failure (bad config, missing dependency, or a
    rejected API call). The CLI turns this into a clean error, never a traceback."""


def require(module_name: str, extra: str):
    """Import an optional dependency by name, or raise an actionable
    IntegrationError telling the user which extra to install."""
    try:
        return __import__(module_name)
    except ImportError as e:
        raise IntegrationError(
            f"The '{extra}' integration needs the '{module_name}' package. "
            f"Install it with:  pip install sqldoc[{extra}]"
        ) from e


def need(config: dict, *keys: str, integration: str):
    """Return the listed config values, raising IntegrationError naming every
    missing key at once (so the user fixes their config in one pass)."""
    missing = [k for k in keys if not config.get(k)]
    if missing:
        raise IntegrationError(
            f"The '{integration}' config is missing required key(s): "
            f"{', '.join(missing)}. Add them under '{integration}:' in .sqldoc.yml."
        )
    return tuple(config.get(k) for k in keys)


@dataclass
class Artifact:
    """One uploadable report: its filename, a coarse kind tag, the raw bytes, and
    a MIME type. Renderers write to bytes so an integration can upload without
    ever touching the local filesystem."""
    name: str
    kind: str          # doc_html | doc_pdf | executive_html | pii_html | pii_json | health_json | metrics_json
    content: bytes
    mime: str = "application/octet-stream"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def __repr__(self):
        return f"Artifact(name={self.name!r}, kind={self.kind!r}, {len(self.content)} bytes)"


@dataclass
class FindingEvent:
    """A single actionable finding surfaced to an issue/ticket tracker
    (Jira, ServiceNow). ``severity`` is one of critical/high/medium/low; ``kind``
    groups events for issue-type routing (pii / health / backup / security /
    schema_change)."""
    kind: str
    severity: str
    title: str
    detail: str
    database: str = ""
    fields: dict = field(default_factory=dict)


def result(ok: bool, detail: str, **extra) -> dict:
    """Uniform result envelope returned by every ``test()``/``push()``."""
    out = {"ok": bool(ok), "detail": detail}
    out.update(extra)
    return out
