"""GitLab project Wiki publisher (REST API v4, personal/project access token).

Maintains one wiki page per database (Markdown), created or updated in place.

Config (``gitlab_wiki:``)::

    gitlab_wiki:
      base_url: https://gitlab.com
      project_id: "123"            # numeric id or URL-encoded path
      token: "***"
"""
from sqldoc.integrations.base import IntegrationError, need, result
from sqldoc.integrations.reports import bundle_markdown


def gitlab_request(method, path, cfg, *, timeout=30.0, **kwargs):
    import requests
    base = (cfg.get("base_url") or "https://gitlab.com").rstrip("/")
    headers = kwargs.pop("headers", {})
    headers.setdefault("PRIVATE-TOKEN", cfg.get("token", ""))
    resp = requests.request(method, f"{base}/api/v4{path}", headers=headers, timeout=timeout, **kwargs)
    if resp.status_code == 404:
        return None
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"GitLab {method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


class Client:
    def __init__(self, config):
        self.cfg = config or {}

    def _pid(self):
        return self.cfg["project_id"]

    def test(self):
        need(self.cfg, "project_id", "token", integration="gitlab_wiki")
        proj = gitlab_request("GET", f"/projects/{self._pid()}", self.cfg)
        if proj is None:
            raise IntegrationError("GitLab project not found (check project_id + token).")
        return result(True, f"Connected to GitLab project '{proj.get('name', self._pid())}'.")

    def push_reports(self, artifacts, metrics=None, bundle=None):
        need(self.cfg, "project_id", "token", integration="gitlab_wiki")
        if bundle is None:
            raise IntegrationError("GitLab Wiki push needs the collected bundle.")
        slug = bundle.database
        content = bundle_markdown(bundle, metrics)
        existing = gitlab_request("GET", f"/projects/{self._pid()}/wikis/{slug}", self.cfg)
        if existing:
            gitlab_request("PUT", f"/projects/{self._pid()}/wikis/{slug}", self.cfg,
                           data={"title": slug, "content": content, "format": "markdown"})
            verb = "Updated"
        else:
            gitlab_request("POST", f"/projects/{self._pid()}/wikis", self.cfg,
                           data={"title": slug, "content": content, "format": "markdown"})
            verb = "Created"
        return result(True, f"{verb} GitLab wiki page '{slug}'.", page=slug)
