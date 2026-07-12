"""SharePoint Online integration via the Microsoft Graph API.

Auth is app-only client-credentials through MSAL: register an Azure AD
application with the ``Sites.ReadWrite.All`` application permission and grant
admin consent. Reports (HTML / PDF / executive / PII, plus the structured JSON)
are uploaded to a document library, and a structured summary row is appended to
a SharePoint List so the metrics are queryable/filterable in SharePoint itself.

Config (``sharepoint:`` in .sqldoc.yml)::

    sharepoint:
      tenant_id: "<aad-tenant-guid>"
      client_id: "<app-client-id>"
      client_secret: "***"
      site_id: "acme.sharepoint.com,<siteGuid>,<webGuid>"
      folder: "Database Documentation"     # library folder for the files
      list_name: "Database Documentation"   # SharePoint List for summary rows

The token acquisition and every HTTP call go through module-level functions so
tests monkeypatch them without a network or a real tenant.
"""
import datetime

from sqldoc.integrations.base import IntegrationError, need, require, result

GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = ["https://graph.microsoft.com/.default"]


def acquire_token(config: dict) -> str:
    """Acquire an app-only Graph token via MSAL client-credentials."""
    msal = require("msal", "sharepoint")
    app = msal.ConfidentialClientApplication(
        client_id=config["client_id"],
        authority=f"https://login.microsoftonline.com/{config['tenant_id']}",
        client_credential=config["client_secret"],
    )
    res = app.acquire_token_for_client(scopes=_SCOPE)
    if "access_token" not in res:
        raise IntegrationError(
            "SharePoint auth failed: "
            + res.get("error_description", res.get("error", "no access_token returned")))
    return res["access_token"]


def graph_request(method: str, url: str, token: str, *, timeout: float = 30.0, **kwargs):
    """Perform a Graph REST call, raising IntegrationError on a non-2xx status."""
    import requests
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {token}")
    resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(
            f"SharePoint Graph {method} {url} -> {resp.status_code}: {resp.text[:300]}")
    if resp.content and resp.headers.get("Content-Type", "").startswith("application/json"):
        return resp.json()
    return {}


class Client:
    def __init__(self, config: dict):
        self.cfg = config or {}

    def _need(self):
        need(self.cfg, "tenant_id", "client_id", "client_secret", "site_id",
             integration="sharepoint")

    def test(self) -> dict:
        """Verify auth + site access; returns the site's display name."""
        self._need()
        token = acquire_token(self.cfg)
        site = graph_request("GET", f"{GRAPH}/sites/{self.cfg['site_id']}", token)
        name = site.get("displayName") or site.get("name") or self.cfg["site_id"]
        return result(True, f"Connected to SharePoint site '{name}'.", site=name)

    # --- upload helpers ----------------------------------------------------

    def _upload_file(self, token, artifact) -> str:
        folder = (self.cfg.get("folder") or "sqldoc").strip("/")
        path = f"{folder}/{artifact.name}"
        url = f"{GRAPH}/sites/{self.cfg['site_id']}/drive/root:/{path}:/content"
        item = graph_request("PUT", url, token,
                             headers={"Content-Type": artifact.mime},
                             data=artifact.content)
        return item.get("webUrl", path)

    def _list_id(self, token) -> str:
        name = self.cfg.get("list_name")
        if not name:
            return None
        data = graph_request(
            "GET", f"{GRAPH}/sites/{self.cfg['site_id']}/lists", token,
            params={"$select": "id,displayName"})
        for lst in data.get("value", []):
            if lst.get("displayName") == name:
                return lst["id"]
        raise IntegrationError(
            f"SharePoint List '{name}' not found on the site. Create it (or fix "
            f"list_name) — its columns should include Database, ScanDate, and the metrics.")

    def _add_list_row(self, token, list_id, metrics, report_url):
        fields = {
            "Title": f"{metrics.get('database')} - {datetime.date.today().isoformat()}",
            "Database": str(metrics.get("database") or ""),
            "ScanDate": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "PIIHigh": metrics.get("pii_high"),
            "PIIFindings": metrics.get("pii_findings"),
            "SecurityScore": metrics.get("security_score"),
            "HealthScore": metrics.get("health_score"),
            "BackupCompliancePct": metrics.get("backup_compliance_pct"),
            "OverallScore": metrics.get("overall_score"),
            "ReportUrl": report_url or "",
        }
        # Drop keys SharePoint would reject as null so a sparse metrics set still posts.
        fields = {k: v for k, v in fields.items() if v is not None}
        graph_request(
            "POST", f"{GRAPH}/sites/{self.cfg['site_id']}/lists/{list_id}/items",
            token, json={"fields": fields})

    def push_reports(self, artifacts, metrics=None, bundle=None) -> dict:
        """Upload every artifact to the document library and, if a list_name is
        configured, append one structured summary row."""
        self._need()
        if not artifacts:
            raise IntegrationError("Nothing to upload (no reports were rendered).")
        token = acquire_token(self.cfg)
        uploaded, primary_url = [], None
        for art in artifacts:
            url = self._upload_file(token, art)
            uploaded.append(art.name)
            if art.kind == "executive_html" or primary_url is None:
                primary_url = url
        list_id = self._list_id(token)
        if list_id and metrics:
            self._add_list_row(token, list_id, metrics, primary_url)
        row = " + 1 list row" if (list_id and metrics) else ""
        return result(True, f"Uploaded {len(uploaded)} report(s){row} to SharePoint.",
                      uploaded=uploaded, url=primary_url)
