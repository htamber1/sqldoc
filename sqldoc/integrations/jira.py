"""Jira integration via the REST API (Cloud v3, API-token / basic auth).

On --push, sqldoc findings that exceed the configured thresholds are turned into
Jira issues, routed to an issue type by finding kind:

* HIGH-risk PII  -> Security   (configurable)
* failed health  -> Bug
* backup stale   -> Task

Each issue carries the full finding detail and (if ``report_url`` is set) a link
back to the sqldoc report. Existing open issues with the same summary are not
duplicated. Thresholds (``security_min`` / ``health_min``) live in the same
section; a HIGH PII finding always fires.

Config (``jira:`` in .sqldoc.yml)::

    jira:
      base_url: https://acme.atlassian.net
      email: bot@acme.com
      api_token: "***"
      project_key: SEC
      issue_types: {pii: Security, health: Bug, backup: Task, security: Security}
      report_url: https://reports.acme.com/db      # optional link back
      security_min: 80
      health_min: 70
"""
from sqldoc.integrations.base import IntegrationError, need, result

_DEFAULT_TYPES = {"pii": "Security", "security": "Security",
                  "health": "Bug", "backup": "Task", "schema_change": "Task"}


def _base(cfg) -> str:
    b = (cfg.get("base_url") or "").rstrip("/")
    if not b:
        raise IntegrationError("jira.base_url is required (e.g. https://acme.atlassian.net).")
    return b


def jira_request(method: str, path: str, cfg: dict, *, timeout: float = 30.0, **kwargs):
    """Jira REST call (path relative to base_url) with token basic auth."""
    import requests
    auth = (cfg["email"], cfg["api_token"])
    headers = kwargs.pop("headers", {})
    headers.setdefault("Accept", "application/json")
    resp = requests.request(method, f"{_base(cfg)}{path}", auth=auth, headers=headers,
                            timeout=timeout, **kwargs)
    if not (200 <= resp.status_code < 300):
        raise IntegrationError(f"Jira {method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


def _adf(text: str) -> dict:
    """Minimal Atlassian Document Format doc (required by the v3 create API)."""
    lines = str(text).split("\n") or [""]
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": (ln or " ")}]}
        for ln in lines]}


def issue_type_for(cfg, kind) -> str:
    mapping = {**_DEFAULT_TYPES, **(cfg.get("issue_types") or {})}
    return mapping.get(kind, "Task")


class Client:
    def __init__(self, config: dict):
        self.cfg = config or {}

    def _need(self):
        need(self.cfg, "base_url", "email", "api_token", "project_key", integration="jira")

    def test(self) -> dict:
        self._need()
        me = jira_request("GET", "/rest/api/3/myself", self.cfg)
        project = jira_request("GET", f"/rest/api/3/project/{self.cfg['project_key']}", self.cfg)
        return result(True, f"Connected to Jira as '{me.get('displayName', me.get('emailAddress'))}'; "
                            f"project '{project.get('name', self.cfg['project_key'])}' reachable.",
                      project=project.get("key"))

    def _open_issue_exists(self, summary) -> bool:
        esc = summary.replace('"', '\\"')
        jql = (f'project = "{self.cfg["project_key"]}" AND summary ~ "{esc}" '
               f'AND statusCategory != Done')
        try:
            data = jira_request("GET", "/rest/api/3/search", self.cfg,
                               params={"jql": jql, "maxResults": 1, "fields": "key"})
        except IntegrationError:
            return False   # search failed -> don't block issue creation
        return bool(data.get("issues"))

    def _description(self, event) -> str:
        body = event.detail
        if self.cfg.get("report_url"):
            body += f"\n\nReport: {self.cfg['report_url']}"
        body += f"\n\n(Raised automatically by sqldoc. Database: {event.database}.)"
        return body

    def create_issues(self, events) -> dict:
        self._need()
        created, skipped = [], 0
        for ev in events:
            if self._open_issue_exists(ev.title):
                skipped += 1
                continue
            fields = {
                "project": {"key": self.cfg["project_key"]},
                "summary": ev.title[:250],
                "issuetype": {"name": issue_type_for(self.cfg, ev.kind)},
                "description": _adf(self._description(ev)),
            }
            issue = jira_request("POST", "/rest/api/3/issue", self.cfg,
                                headers={"Content-Type": "application/json"},
                                json={"fields": fields})
            created.append(issue.get("key"))
        detail = f"Created {len(created)} Jira issue(s)"
        if skipped:
            detail += f"; skipped {skipped} already-open duplicate(s)"
        url = None
        if created:
            url = f"{_base(self.cfg)}/browse/{created[0]}"
        return result(True, detail + ".", created=created, skipped=skipped, url=url)
