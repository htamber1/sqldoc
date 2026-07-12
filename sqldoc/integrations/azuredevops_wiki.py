"""Azure DevOps project Wiki publisher (REST API, PAT auth).

Maintains one wiki page per database (Markdown), created or updated in place
(ETag-guarded). Reuses the azuredevops org/project/pat config.

Config (``azuredevops_wiki:``)::

    azuredevops_wiki:
      organization: acme          # or org_url
      project: Data
      pat: "***"
      wiki: Data.wiki             # wiki identifier
"""
from sqldoc.integrations.base import IntegrationError, need, result
from sqldoc.integrations.azuredevops import _org_url
from sqldoc.integrations.reports import bundle_markdown

_API = "api-version=7.0"


def wiki_request(method, url, cfg, *, timeout=30.0, **kwargs):
    """Returns (status, json, etag). 404 -> (404, None, None)."""
    import requests
    resp = requests.request(method, url, auth=("", cfg["pat"]),
                            headers=kwargs.pop("headers", {"Accept": "application/json"}),
                            timeout=timeout, **kwargs)
    if resp.status_code == 404:
        return 404, None, None
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Azure DevOps Wiki {method} {url} -> {resp.status_code}: {resp.text[:300]}")
    return resp.status_code, (resp.json() if resp.content else {}), resp.headers.get("ETag")


class Client:
    def __init__(self, config):
        self.cfg = config or {}

    def _wiki(self):
        return self.cfg.get("wiki") or f"{self.cfg['project']}.wiki"

    def _base(self):
        return f"{_org_url(self.cfg)}/{self.cfg['project']}/_apis/wiki/wikis/{self._wiki()}"

    def test(self):
        need(self.cfg, "project", "pat", integration="azuredevops_wiki")
        _org_url(self.cfg)
        status, data, _ = wiki_request("GET", f"{self._base()}?{_API}", self.cfg)
        if status == 404:
            raise IntegrationError(f"Azure DevOps wiki '{self._wiki()}' not found.")
        return result(True, f"Connected to Azure DevOps wiki '{self._wiki()}'.")

    def push_reports(self, artifacts, metrics=None, bundle=None):
        need(self.cfg, "project", "pat", integration="azuredevops_wiki")
        if bundle is None:
            raise IntegrationError("Azure DevOps Wiki push needs the collected bundle.")
        path = f"/{bundle.database}"
        content = bundle_markdown(bundle, metrics)
        page_url = f"{self._base()}/pages?path={path}&{_API}"
        status, _data, etag = wiki_request("GET", page_url, self.cfg)
        headers = {"Content-Type": "application/json"}
        if status != 404 and etag:
            headers["If-Match"] = etag
            verb = "Updated"
        else:
            verb = "Created"
        wiki_request("PUT", page_url, self.cfg, headers=headers, json={"content": content})
        return result(True, f"{verb} Azure DevOps wiki page '{path}'.", page=path)
