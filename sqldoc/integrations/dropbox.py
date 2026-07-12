"""Dropbox (Business) publisher via the Dropbox API v2.

Uploads the rendered reports to a Dropbox folder (overwriting for version
history). Uses a Dropbox access token (a Business team member token works).

Config (``dropbox:``)::

    dropbox:
      token: "***"
      folder: "/Database Documentation"
"""
import json as _json

from sqldoc.integrations.base import IntegrationError, need, result


def dropbox_rpc(endpoint, cfg, body, *, timeout=30.0):
    import requests
    resp = requests.post(f"https://api.dropboxapi.com/2/{endpoint}",
                         headers={"Authorization": f"Bearer {cfg['token']}",
                                  "Content-Type": "application/json"},
                         json=body, timeout=timeout)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Dropbox {endpoint} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


def dropbox_upload(cfg, path, content, *, timeout=60.0):
    import requests
    arg = _json.dumps({"path": path, "mode": "overwrite", "mute": True})
    resp = requests.post("https://content.dropboxapi.com/2/files/upload",
                         headers={"Authorization": f"Bearer {cfg['token']}",
                                  "Dropbox-API-Arg": arg,
                                  "Content-Type": "application/octet-stream"},
                         data=content, timeout=timeout)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Dropbox upload {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


class Client:
    def __init__(self, config):
        self.cfg = config or {}

    def test(self):
        need(self.cfg, "token", integration="dropbox")
        acct = dropbox_rpc("users/get_current_account", self.cfg, None)
        name = (acct.get("name") or {}).get("display_name", "account")
        return result(True, f"Connected to Dropbox as '{name}'.")

    def push_reports(self, artifacts, metrics=None, bundle=None):
        need(self.cfg, "token", integration="dropbox")
        if not artifacts:
            raise IntegrationError("Nothing to upload (no reports were rendered).")
        folder = (self.cfg.get("folder") or "/sqldoc").rstrip("/")
        if not folder.startswith("/"):
            folder = "/" + folder
        uploaded = []
        for art in artifacts:
            dropbox_upload(self.cfg, f"{folder}/{art.name}", art.content)
            uploaded.append(art.name)
        return result(True, f"Uploaded {len(uploaded)} report(s) to Dropbox ({folder}).",
                      uploaded=uploaded)
