"""ServiceNow integration via the REST Table API (basic auth).

* Critical findings (security score below threshold, failed health, backup
  staleness, HIGH PII) become **incidents**, with urgency/impact set from
  severity.
* Schema changes on a production database become **change requests**
  (``create_change_request`` — driven by the agent's schema-change poller).
* The database's **configuration item** (CMDB CI) is updated with documentation
  metadata (last scan date + object/finding counts) when a ``ci_class`` is set.

Config (``servicenow:`` in .sqldoc.yml)::

    servicenow:
      instance_url: https://acme.service-now.com
      username: svc_sqldoc
      password: "***"
      ci_class: cmdb_ci_database         # optional, for CI updates
      security_min: 80
      production_databases: [PROD, DW]   # which DBs get change requests
"""
import datetime

from sqldoc.integrations.base import IntegrationError, need, result

# severity -> (urgency, impact); ServiceNow scale is 1 (high) .. 3 (low).
_SEV = {"critical": (1, 1), "high": (2, 1), "medium": (3, 2), "low": (3, 3)}


def _base(cfg) -> str:
    b = (cfg.get("instance_url") or "").rstrip("/")
    if not b:
        raise IntegrationError("servicenow.instance_url is required "
                               "(e.g. https://acme.service-now.com).")
    return b


def sn_request(method: str, path: str, cfg: dict, *, timeout: float = 30.0, **kwargs):
    """ServiceNow Table API call (path relative to instance_url) with basic auth."""
    import requests
    auth = (cfg["username"], cfg["password"])
    headers = kwargs.pop("headers", {})
    headers.setdefault("Accept", "application/json")
    resp = requests.request(method, f"{_base(cfg)}{path}", auth=auth, headers=headers,
                            timeout=timeout, **kwargs)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"ServiceNow {method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


class Client:
    def __init__(self, config: dict):
        self.cfg = config or {}

    def _need(self):
        need(self.cfg, "instance_url", "username", "password", integration="servicenow")

    def test(self) -> dict:
        self._need()
        # A limit-1 read verifies credentials + table access.
        sn_request("GET", "/api/now/table/incident", self.cfg,
                   params={"sysparm_limit": 1, "sysparm_fields": "sys_id"})
        return result(True, f"Connected to ServiceNow at {_base(self.cfg)}.")

    def create_incident(self, event) -> str:
        urgency, impact = _SEV.get(event.severity, (3, 2))
        body = {
            "short_description": event.title[:160],
            "description": event.detail + f"\n\n(Raised by sqldoc. Database: {event.database}.)",
            "urgency": urgency, "impact": impact,
            "category": "Database",
        }
        res = sn_request("POST", "/api/now/table/incident", self.cfg,
                        headers={"Content-Type": "application/json"}, json=body)
        return (res.get("result") or {}).get("number") or (res.get("result") or {}).get("sys_id")

    def create_change_request(self, database, description, risk="moderate") -> str:
        """Raise a change request for a schema change on a production database."""
        self._need()
        body = {
            "short_description": f"[sqldoc] Schema change on production database {database}",
            "description": description,
            "type": "normal", "risk": risk, "category": "Database",
        }
        res = sn_request("POST", "/api/now/table/change_request", self.cfg,
                        headers={"Content-Type": "application/json"}, json=body)
        return (res.get("result") or {}).get("number")

    def update_ci(self, database, metadata) -> bool:
        """Update the database's CMDB CI with documentation metadata. Best-effort:
        returns False if no CI class is configured or the CI isn't found."""
        ci_class = self.cfg.get("ci_class")
        if not ci_class:
            return False
        found = sn_request("GET", f"/api/now/table/{ci_class}", self.cfg,
                          params={"sysparm_query": f"name={database}", "sysparm_limit": 1,
                                  "sysparm_fields": "sys_id,name"})
        rows = found.get("result") or []
        if not rows:
            return False
        note = (f"sqldoc scan {datetime.date.today().isoformat()}: "
                f"{metadata.get('tables', '?')} tables, {metadata.get('pii_findings', '?')} PII, "
                f"security {metadata.get('security_score', 'N/A')}, "
                f"health {metadata.get('health_score', 'N/A')}.")
        sn_request("PATCH", f"/api/now/table/{ci_class}/{rows[0]['sys_id']}", self.cfg,
                   headers={"Content-Type": "application/json"},
                   json={"short_description": note})
        return True

    def create_issues(self, events, metrics=None) -> dict:
        """Factory --push entry: create an incident per finding event, then update
        the database's CI record with documentation metadata (even when no
        incident fires — the CI note reflects every scan)."""
        self._need()
        created = [self.create_incident(ev) for ev in events]
        database = metrics.get("database") if metrics else (events[0].database if events else None)
        ci_updated = False
        if database and self.cfg.get("ci_class"):
            try:
                ci_updated = self.update_ci(database, metrics or {})
            except IntegrationError:
                ci_updated = False
        detail = f"Created {len(created)} ServiceNow incident(s)"
        if ci_updated:
            detail += "; updated CI record"
        return result(True, detail + ".", created=created, ci_updated=ci_updated)
