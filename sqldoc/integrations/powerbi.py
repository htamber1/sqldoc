"""Power BI integration — push sqldoc metrics to a streaming dataset.

Executives build live dashboards on top of the pushed rows (health/PII/backup/
security scores over time). Two auth paths:

* **push_url** — the streaming dataset's push URL (key embedded); rows are POSTed
  directly, no Azure AD needed. Simplest.
* **Azure AD** — service principal via MSAL (tenant_id/client_id/client_secret)
  posting to the Power BI REST API for a workspace + dataset.

Config (``powerbi:`` in .sqldoc.yml)::

    powerbi:
      push_url: "https://api.powerbi.com/beta/<ws>/datasets/<id>/rows?key=..."
    # --- or the REST API path ---
      tenant_id: "..."
      client_id: "..."
      client_secret: "***"
      group_id: "<workspace-id>"
      dataset_id: "<dataset-id>"
"""
import datetime

from sqldoc.integrations.base import IntegrationError, need, require, result

_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
_API = "https://api.powerbi.com/v1.0/myorg"


def acquire_token(cfg) -> str:
    """Acquire a Power BI REST token via MSAL client-credentials."""
    msal = require("msal", "powerbi")
    app = msal.ConfidentialClientApplication(
        client_id=cfg["client_id"],
        authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
        client_credential=cfg["client_secret"])
    res = app.acquire_token_for_client(scopes=_SCOPE)
    if "access_token" not in res:
        raise IntegrationError("Power BI auth failed: "
                               + res.get("error_description", res.get("error", "no token")))
    return res["access_token"]


def post_rows_url(url: str, rows: list, *, timeout: float = 30.0):
    """POST rows to a streaming-dataset push URL (key embedded in the URL)."""
    import requests
    resp = requests.post(url, json={"rows": rows}, timeout=timeout)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Power BI push -> {resp.status_code}: {resp.text[:300]}")


def api_request(method: str, url: str, token: str, *, timeout: float = 30.0, **kwargs):
    import requests
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {token}")
    resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Power BI {method} {url} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


def _row(metrics) -> dict:
    m = metrics or {}
    row = {
        "database": str(m.get("database", "")),
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
                     .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "pii_high": m.get("pii_high"),
        "pii_findings": m.get("pii_findings"),
        "security_score": m.get("security_score"),
        "health_score": m.get("health_score"),
        "backup_compliance_pct": m.get("backup_compliance_pct"),
        "overall_score": m.get("overall_score"),
    }
    # Streaming datasets take missing keys fine but reject explicit nulls on typed
    # numeric columns; drop the Nones.
    return {k: v for k, v in row.items() if v is not None}


class Client:
    def __init__(self, config: dict):
        self.cfg = config or {}

    def _uses_push_url(self) -> bool:
        return bool(self.cfg.get("push_url"))

    def test(self) -> dict:
        if self._uses_push_url():
            post_rows_url(self.cfg["push_url"], [])   # empty batch validates the URL
            return result(True, "Power BI streaming dataset reachable (push URL).")
        need(self.cfg, "tenant_id", "client_id", "client_secret", "group_id", "dataset_id",
             integration="powerbi")
        token = acquire_token(self.cfg)
        ds = api_request("GET", f"{_API}/groups/{self.cfg['group_id']}/datasets/"
                        f"{self.cfg['dataset_id']}", token)
        return result(True, f"Connected to Power BI dataset "
                            f"'{ds.get('name', self.cfg['dataset_id'])}'.", dataset=ds.get("name"))

    def push_metrics(self, metrics) -> dict:
        row = _row(metrics)
        if self._uses_push_url():
            post_rows_url(self.cfg["push_url"], [row])
        else:
            need(self.cfg, "tenant_id", "client_id", "client_secret", "group_id", "dataset_id",
                 integration="powerbi")
            token = acquire_token(self.cfg)
            api_request("POST", f"{_API}/groups/{self.cfg['group_id']}/datasets/"
                       f"{self.cfg['dataset_id']}/rows", token,
                       headers={"Content-Type": "application/json"}, json={"rows": [row]})
        return result(True, f"Pushed metrics row for '{row.get('database', '?')}' to Power BI.",
                      row=row)
