"""Nuclino publisher via the Nuclino API.

Creates or updates one Nuclino item per database (Markdown content). To update
in place, map databases to item ids under ``items``; otherwise a new item is
created in the configured workspace.

Config (``nuclino:``)::

    nuclino:
      api_key: "***"
      workspace_id: "<workspace-id>"
      items: {Sales: "<item-id>"}     # optional: db -> existing item to update
"""
from sqldoc.integrations.base import IntegrationError, need, result
from sqldoc.integrations.reports import bundle_markdown

_API = "https://api.nuclino.com/v0"


def nuclino_request(method, path, cfg, *, timeout=30.0, **kwargs):
    import requests
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", cfg.get("api_key", ""))
    headers.setdefault("Content-Type", "application/json")
    resp = requests.request(method, f"{_API}{path}", headers=headers, timeout=timeout, **kwargs)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Nuclino {method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


class Client:
    def __init__(self, config):
        self.cfg = config or {}

    def test(self):
        need(self.cfg, "api_key", integration="nuclino")
        nuclino_request("GET", "/workspaces", self.cfg)
        return result(True, "Connected to Nuclino.")

    def push_reports(self, artifacts, metrics=None, bundle=None):
        need(self.cfg, "api_key", integration="nuclino")
        if bundle is None:
            raise IntegrationError("Nuclino push needs the collected bundle.")
        content = bundle_markdown(bundle, metrics)
        title = f"Database: {bundle.database}"
        item_id = (self.cfg.get("items") or {}).get(bundle.database)
        if item_id:
            data = nuclino_request("PUT", f"/items/{item_id}", self.cfg,
                                   json={"title": title, "content": content})
            verb = "Updated"
        else:
            need(self.cfg, "workspace_id", integration="nuclino")
            data = nuclino_request("POST", "/items", self.cfg,
                                   json={"workspaceId": self.cfg["workspace_id"],
                                         "title": title, "content": content})
            verb = "Created"
        return result(True, f"{verb} Nuclino item for '{bundle.database}'.",
                      url=(data.get("data") or data).get("url") if isinstance(data, dict) else None)
