"""OneDrive publisher via Microsoft Graph (reuses SharePoint MSAL app auth).

Uploads the rendered reports to a OneDrive folder. Works against a specific
drive (``drive_id``) or a user's drive (``user_id``).

Config (``onedrive:``)::

    onedrive:
      tenant_id: "..."
      client_id: "..."
      client_secret: "***"
      user_id: "user@acme.com"     # or drive_id
      folder: "Database Documentation"
"""
from sqldoc.integrations.base import IntegrationError, need, result
from sqldoc.integrations.sharepoint import acquire_token, graph_request, GRAPH


class Client:
    def __init__(self, config):
        self.cfg = config or {}

    def _drive_base(self):
        if self.cfg.get("drive_id"):
            return f"{GRAPH}/drives/{self.cfg['drive_id']}"
        if self.cfg.get("user_id"):
            return f"{GRAPH}/users/{self.cfg['user_id']}/drive"
        raise IntegrationError("onedrive needs drive_id or user_id.")

    def test(self):
        need(self.cfg, "tenant_id", "client_id", "client_secret", integration="onedrive")
        token = acquire_token(self.cfg)
        drive = graph_request("GET", self._drive_base(), token)
        return result(True, f"Connected to OneDrive "
                            f"'{drive.get('name', drive.get('id', 'drive'))}'.")

    def push_reports(self, artifacts, metrics=None, bundle=None):
        need(self.cfg, "tenant_id", "client_id", "client_secret", integration="onedrive")
        if not artifacts:
            raise IntegrationError("Nothing to upload (no reports were rendered).")
        token = acquire_token(self.cfg)
        folder = (self.cfg.get("folder") or "sqldoc").strip("/")
        base = self._drive_base()
        uploaded, primary = [], None
        for art in artifacts:
            url = f"{base}/root:/{folder}/{art.name}:/content"
            item = graph_request("PUT", url, token,
                                 headers={"Content-Type": art.mime}, data=art.content)
            uploaded.append(art.name)
            if art.kind == "executive_html" or primary is None:
                primary = item.get("webUrl")
        return result(True, f"Uploaded {len(uploaded)} report(s) to OneDrive.",
                      uploaded=uploaded, url=primary)
