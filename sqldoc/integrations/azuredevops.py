"""Azure DevOps integration via the REST API (PAT auth).

--push does two things for a database:

* uploads the rendered reports as **work-item attachments** on a per-database
  "Database documentation" tracking work item (created once, reused on re-push) —
  the reports live in Azure DevOps, versioned by the item's history;
* creates **work items** for findings that exceed thresholds, routed by kind and
  de-duplicated against open items.

Inside a pipeline, the shipped template (``azure-pipelines/sqldoc.yml``) runs
sqldoc as a task and publishes the reports as native **pipeline artifacts** — the
Azure DevOps alternative to the GitHub Action.

Config (``azuredevops:`` in .sqldoc.yml)::

    azuredevops:
      organization: acme            # or org_url: https://dev.azure.com/acme
      project: Data
      pat: "***"
      work_item_type: Issue         # findings
      doc_work_item_type: Task      # the doc tracking item
      security_min: 80
      health_min: 70
"""
from sqldoc.integrations.base import IntegrationError, need, result

_API = "api-version=7.0"
_DEFAULT_TYPES = {"pii": "Issue", "security": "Issue", "health": "Bug",
                  "backup": "Task", "schema_change": "Task"}


def _org_url(cfg) -> str:
    if cfg.get("org_url"):
        return cfg["org_url"].rstrip("/")
    if cfg.get("organization"):
        return f"https://dev.azure.com/{cfg['organization']}"
    raise IntegrationError("azuredevops needs 'organization' or 'org_url'.")


def _proj(cfg) -> str:
    return f"{_org_url(cfg)}/{cfg['project']}"


def ado_request(method: str, url: str, cfg: dict, *, timeout: float = 30.0, **kwargs):
    """Azure DevOps REST call with PAT basic auth; raise IntegrationError on non-2xx."""
    import requests
    auth = ("", cfg["pat"])
    headers = kwargs.pop("headers", {})
    headers.setdefault("Accept", "application/json")
    resp = requests.request(method, url, auth=auth, headers=headers, timeout=timeout, **kwargs)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Azure DevOps {method} {url} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


def work_item_type_for(cfg, kind) -> str:
    mapping = {**_DEFAULT_TYPES, **(cfg.get("work_item_type_map") or {})}
    if cfg.get("work_item_type"):
        # A single override applies to finding items; the doc item uses its own.
        mapping = {k: cfg["work_item_type"] for k in mapping}
        mapping.update(cfg.get("work_item_type_map") or {})
    return mapping.get(kind, "Issue")


class Client:
    def __init__(self, config: dict):
        self.cfg = config or {}

    def _need(self):
        need(self.cfg, "project", "pat", integration="azuredevops")
        _org_url(self.cfg)   # validates organization/org_url

    def test(self) -> dict:
        self._need()
        proj = ado_request("GET", f"{_org_url(self.cfg)}/_apis/projects/"
                          f"{self.cfg['project']}?{_API}", self.cfg)
        return result(True, f"Connected to Azure DevOps project "
                            f"'{proj.get('name', self.cfg['project'])}'.", project=proj.get("name"))

    # --- work items --------------------------------------------------------

    def _find_open_item(self, title):
        wiql = {"query": "SELECT [System.Id] FROM workitems WHERE "  # nosec B608 - WIQL (Azure DevOps, not DB SQL); title single-quote-escaped
                         f"[System.Title] = '{title.replace(chr(39), chr(39) * 2)}' "
                         "AND [System.State] <> 'Closed' "
                         "AND [System.State] <> 'Done'"}
        data = ado_request("POST", f"{_proj(self.cfg)}/_apis/wit/wiql?{_API}", self.cfg,
                          headers={"Content-Type": "application/json"}, json=wiql)
        items = data.get("workItems", [])
        return items[0]["id"] if items else None

    def _create_work_item(self, wtype, title, description):
        body = [
            {"op": "add", "path": "/fields/System.Title", "value": title[:250]},
            {"op": "add", "path": "/fields/System.Description", "value": description},
        ]
        if self.cfg.get("area_path"):
            body.append({"op": "add", "path": "/fields/System.AreaPath", "value": self.cfg["area_path"]})
        item = ado_request(
            "POST", f"{_proj(self.cfg)}/_apis/wit/workitems/${wtype}?{_API}", self.cfg,
            headers={"Content-Type": "application/json-patch+json"}, json=body)
        return item["id"]

    def _upload_attachment(self, name, content):
        att = ado_request(
            "POST", f"{_proj(self.cfg)}/_apis/wit/attachments?fileName={name}&{_API}", self.cfg,
            headers={"Content-Type": "application/octet-stream"}, data=content)
        return att["url"]

    def _attach_to_item(self, item_id, url, comment):
        body = [{"op": "add", "path": "/relations/-",
                 "value": {"rel": "AttachedFile", "url": url,
                           "attributes": {"comment": comment}}}]
        ado_request("PATCH", f"{_proj(self.cfg)}/_apis/wit/workitems/{item_id}?{_API}", self.cfg,
                    headers={"Content-Type": "application/json-patch+json"}, json=body)

    def push_reports(self, artifacts, metrics=None, bundle=None) -> dict:
        from sqldoc.integrations.reports import finding_events
        self._need()
        database = (metrics or {}).get("database") or (bundle.database if bundle else "database")

        # 1) doc tracking work item (reused across pushes) + report attachments.
        doc_title = f"Database documentation: {database}"
        doc_type = self.cfg.get("doc_work_item_type", "Task")
        item_id = self._find_open_item(doc_title)
        if item_id is None:
            item_id = self._create_work_item(
                doc_type, doc_title,
                f"sqldoc documentation for {database}. Reports attached below; "
                f"refreshed on each run.")
        attached = 0
        for art in artifacts:
            url = self._upload_attachment(art.name, art.content)
            self._attach_to_item(item_id, url, "sqldoc report")
            attached += 1

        # 2) finding work items (deduped against open items).
        events = finding_events(bundle, self._thresholds()) if bundle else []
        created, skipped = [], 0
        for ev in events:
            if self._find_open_item(ev.title):
                skipped += 1
                continue
            created.append(self._create_work_item(
                work_item_type_for(self.cfg, ev.kind), ev.title, ev.detail))

        detail = (f"Attached {attached} report(s) to work item #{item_id}; "
                  f"created {len(created)} finding work item(s)")
        if skipped:
            detail += f"; skipped {skipped} open duplicate(s)"
        url = f"{_proj(self.cfg)}/_workitems/edit/{item_id}"
        return result(True, detail + ".", work_item=item_id, created=created, url=url)

    def _thresholds(self):
        out = {}
        for k in ("security_min", "health_min", "backup_max_age_hours"):
            if self.cfg.get(k) is not None:
                out[k] = self.cfg[k]
        return out
