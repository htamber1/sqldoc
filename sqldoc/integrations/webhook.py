"""Generic HTTP webhook — a catch-all for any system not explicitly supported.

--push POSTs a JSON payload built from the scan to a configured URL. The payload
is either sqldoc's default envelope or a **custom template** you supply: any
string in the template is filled from the scan context with ``{placeholder}``
substitution (missing placeholders resolve to empty), so you can shape the body
to whatever the receiving system expects.

Context available to templates: ``database``, ``timestamp``, ``tables``,
``pii_findings``, ``pii_high``, ``security_score``, ``health_score``,
``backup_compliance_pct``, ``overall_score``, ``artifact_names``.

Config (``webhook:`` in .sqldoc.yml)::

    webhook:
      url: https://example.com/hooks/sqldoc
      method: POST
      headers: {Authorization: "Bearer ***"}
      payload_template:                 # optional; omit for the default envelope
        event: "database.scanned"
        db: "{database}"
        risk_score: "{overall_score}"
        highPII: "{pii_high}"
"""
import datetime

from sqldoc.integrations.base import IntegrationError, need, result


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def post(url, method, headers, payload, timeout: float = 30.0):
    """Module-level transport (mockable). Raises IntegrationError on non-2xx."""
    import requests
    resp = requests.request(method or "POST", url, headers=headers or {},
                            json=payload, timeout=timeout)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Webhook {method} {url} -> {resp.status_code}: {resp.text[:300]}")
    return resp


def _context(metrics, bundle, artifacts) -> dict:
    m = dict(metrics or {})
    m.setdefault("database", bundle.database if bundle else "")
    m["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    m["artifact_names"] = [a.name for a in (artifacts or [])]
    return m


def render_template(template, context):
    """Recursively fill string leaves of a template with {placeholder} values."""
    if isinstance(template, str):
        return template.format_map(_SafeDict(context))
    if isinstance(template, dict):
        return {k: render_template(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [render_template(v, context) for v in template]
    return template


def _default_payload(context, bundle) -> dict:
    from sqldoc.pii import summarize as pii_summarize
    payload = {
        "event": "sqldoc.report",
        "database": context.get("database"),
        "timestamp": context["timestamp"],
        "metrics": {k: v for k, v in context.items()
                    if k not in ("timestamp", "artifact_names")},
        "artifacts": context.get("artifact_names", []),
    }
    if bundle is not None:
        payload["pii_summary"] = pii_summarize(bundle.findings)
    return payload


class Client:
    def __init__(self, config: dict):
        self.cfg = config or {}

    def test(self) -> dict:
        need(self.cfg, "url", integration="webhook")
        post(self.cfg["url"], self.cfg.get("method", "POST"), self.cfg.get("headers"),
             {"event": "sqldoc.test",
              "timestamp": datetime.datetime.now(datetime.timezone.utc)
                           .isoformat(timespec="seconds").replace("+00:00", "Z")})
        return result(True, f"Webhook at {self.cfg['url']} accepted a test payload.")

    def push_reports(self, artifacts, metrics=None, bundle=None) -> dict:
        need(self.cfg, "url", integration="webhook")
        context = _context(metrics, bundle, artifacts)
        template = self.cfg.get("payload_template")
        payload = render_template(template, context) if template else _default_payload(context, bundle)
        post(self.cfg["url"], self.cfg.get("method", "POST"), self.cfg.get("headers"), payload)
        shape = "custom" if template else "default"
        return result(True, f"Posted {shape} payload for '{context.get('database')}' "
                            f"to the webhook.", url=self.cfg["url"])
