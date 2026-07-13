"""Access-request intake from many sources, all funneled through one pipeline.

Whatever the source — a forwarded email, a ServiceNow request, an Azure DevOps
work item, a GitHub issue, or a REST POST — it becomes an :class:`IntakeItem`,
the request details are extracted (AI, with a heuristic fallback), and the same
check → gap → script-generation workflow runs. Fetchers reuse the existing
integration transports and are best-effort.
"""
from dataclasses import dataclass, field
from email import message_from_string

from sqldoc.access.model import ParsedRequest


@dataclass
class IntakeItem:
    source: str
    id: str = ""
    title: str = ""
    body: str = ""
    requester_hint: str = ""
    url: str = ""


@dataclass
class RequestOutcome:
    item: IntakeItem = None
    extracted: object = None      # TicketRequest
    report: object = None
    parsed: ParsedRequest = None
    gap: object = None
    script: object = None
    note: str = ""


# --- shared workflow -------------------------------------------------------

def _tables_for(cfg, database):
    from sqldoc.access import config as access_config
    from sqldoc.access.checker import build_db_adapter
    from sqldoc.pii import scan_tables
    for entry in access_config.servers(cfg):
        if any(db.lower() == (database or "").lower() for db in entry["databases"]):
            dialect = entry.get("dialect", "sqlserver")
            try:
                adapter = build_db_adapter(entry, database)
                tables = adapter.extract_metadata()
                return tables, scan_tables(tables), entry["name"], dialect
            except Exception:
                return [], [], entry["name"], dialect
    return [], [], "", "sqlserver"


def run_request(cfg, user, database, level, mode="local", model=None, backend=None) -> RequestOutcome:
    """The shared check → gap → script pipeline for one resolved request."""
    from sqldoc.access.checker import check_access
    from sqldoc.access.gap import analyze_gap
    from sqldoc.access.script import generate_script
    report = check_access(cfg, user)
    parsed = ParsedRequest(raw=f"{level} access to {database}", database=database, level=level or "read")
    gap = analyze_gap(parsed, report)
    tables, pii, server_name, dialect = _tables_for(cfg, database)
    script = generate_script(report, parsed, server_name or "(server)", database,
                             tables=tables, pii_findings=pii, dialect=dialect)
    return RequestOutcome(report=report, parsed=parsed, gap=gap, script=script)


def process_item(cfg, item, user_override=None, mode="local", model=None,
                 backend=None, no_ai=False) -> RequestOutcome:
    """Extract a request from an intake item and run the full workflow."""
    from sqldoc.access import config as access_config
    from sqldoc.access.jira_flow import extract_request
    known = [db for s in access_config.servers(cfg) for db in s["databases"]]
    extracted = extract_request(item.title, item.body, known_databases=known,
                                mode=mode, model=model, backend=backend, no_ai=no_ai)
    if user_override:
        extracted.user = user_override
    if not extracted.user and item.requester_hint:
        extracted.user = item.requester_hint
    if not extracted.user or not extracted.database:
        return RequestOutcome(item=item, extracted=extracted,
                              note="Could not determine the user and/or database from the item.")
    outcome = run_request(cfg, extracted.user, extracted.database, extracted.level,
                          mode=mode, model=model, backend=backend)
    outcome.item = item
    outcome.extracted = extracted
    return outcome


# --- email intake ----------------------------------------------------------

def parse_email(raw: str) -> IntakeItem:
    """Parse a forwarded/raw email into an IntakeItem (subject + text body)."""
    msg = message_from_string(raw or "")
    subject = msg.get("Subject", "")
    sender = msg.get("From", "")
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body_parts.append(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"))
                except Exception:
                    body_parts.append(str(part.get_payload()))
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            body_parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
        else:
            body_parts.append(str(msg.get_payload()))
    body = "\n".join(body_parts).strip() or (raw or "")
    # A forwarded email often carries the original requester after "From:".
    import re
    hint = ""
    m = re.search(r"[\w.\-]+@[\w.\-]+", sender or body)
    if m:
        hint = m.group(0)
    return IntakeItem(source="email", title=subject, body=body, requester_hint=hint)


# --- ServiceNow intake -----------------------------------------------------

def from_servicenow(cfg, table=None, query=None, limit=50) -> list:
    """Pull access requests from a ServiceNow table (default sc_request)."""
    from sqldoc.integrations.servicenow import sn_request
    from sqldoc.integrations import section
    sn_cfg = section(cfg, "servicenow")
    intake = _intake_cfg(cfg).get("servicenow", {})
    table = table or intake.get("table", "sc_request")
    query = query or intake.get("query", "active=true")
    data = sn_request("GET", f"/api/now/table/{table}", sn_cfg,
                      params={"sysparm_query": query, "sysparm_limit": limit})
    items = []
    for r in (data or {}).get("result", []):
        items.append(IntakeItem(
            source="servicenow", id=r.get("number") or r.get("sys_id", ""),
            title=r.get("short_description", ""),
            body=r.get("description", "") or r.get("comments", ""),
            requester_hint=_sn_ref(r.get("requested_for") or r.get("caller_id"))))
    return items


def _sn_ref(v):
    if isinstance(v, dict):
        return v.get("display_value") or v.get("value") or ""
    return v or ""


# --- Azure DevOps intake ---------------------------------------------------

def from_azuredevops(cfg, tag=None, limit=50) -> list:
    """Pull work items tagged as access requests from Azure DevOps."""
    from sqldoc.integrations.azuredevops import ado_request, _proj, _API
    from sqldoc.integrations import section
    ado_cfg = section(cfg, "azuredevops")
    tag = tag or _intake_cfg(cfg).get("azuredevops", {}).get("tag", "access-request")
    # Escape single quotes for the WIQL string literal (defense-in-depth; the tag
    # comes from config or --tag, not an untrusted caller, but never trust input).
    tag_lit = str(tag).replace("'", "''")
    wiql = {"query": "SELECT [System.Id] FROM workitems WHERE "  # nosec B608 - WIQL (Azure DevOps, not DB SQL); tag single-quote-escaped
                     f"[System.Tags] CONTAINS '{tag_lit}' AND [System.State] <> 'Closed' "
                     "AND [System.State] <> 'Done'"}
    data = ado_request("POST", f"{_proj(ado_cfg)}/_apis/wit/wiql?{_API}", ado_cfg,
                       headers={"Content-Type": "application/json"}, json=wiql)
    items = []
    for wi in (data.get("workItems") or [])[:limit]:
        detail = ado_request("GET", f"{_proj(ado_cfg)}/_apis/wit/workitems/{wi['id']}?{_API}", ado_cfg)
        fields = detail.get("fields", {})
        items.append(IntakeItem(
            source="azuredevops", id=str(wi["id"]),
            title=fields.get("System.Title", ""),
            body=_strip_html(fields.get("System.Description", "")),
            requester_hint=_ado_ref(fields.get("System.CreatedBy"))))
    return items


def _ado_ref(v):
    if isinstance(v, dict):
        return v.get("uniqueName") or v.get("displayName") or ""
    return v or ""


def _strip_html(s):
    import re
    return re.sub(r"<[^>]+>", " ", s or "").strip()


# --- GitHub Issues intake --------------------------------------------------

def github_request(method, path, cfg, *, timeout=30.0, **kwargs):
    import requests
    headers = kwargs.pop("headers", {})
    if cfg.get("token"):
        headers.setdefault("Authorization", f"Bearer {cfg['token']}")
    headers.setdefault("Accept", "application/vnd.github+json")
    base = (cfg.get("api_url") or "https://api.github.com").rstrip("/")
    resp = requests.request(method, f"{base}{path}", headers=headers, timeout=timeout, **kwargs)
    if not (200 <= resp.status_code < 300):
        from sqldoc.integrations.base import IntegrationError
        raise IntegrationError(f"GitHub {method} {path} -> {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.content else {}


def from_github(cfg, repo=None, label=None, limit=50) -> list:
    """Pull open GitHub issues (optionally by label) as access requests."""
    gh = _intake_cfg(cfg).get("github", {}) or {}
    repo = repo or gh.get("repo")
    label = label or gh.get("label", "access-request")
    if not repo:
        from sqldoc.integrations.base import IntegrationError
        raise IntegrationError("GitHub intake needs access.intake.github.repo (owner/name).")
    params = {"state": "open", "per_page": limit}
    if label:
        params["labels"] = label
    data = github_request("GET", f"/repos/{repo}/issues", gh, params=params)
    items = []
    for issue in data or []:
        if "pull_request" in issue:      # the issues endpoint also returns PRs
            continue
        items.append(IntakeItem(
            source="github", id=str(issue.get("number", "")),
            title=issue.get("title", ""), body=issue.get("body", "") or "",
            requester_hint=(issue.get("user") or {}).get("login", ""),
            url=issue.get("html_url", "")))
    return items


# --- config helper ---------------------------------------------------------

def _intake_cfg(cfg):
    from sqldoc.access import config as access_config
    return access_config.section(cfg).get("intake") or {}


FETCHERS = {
    "servicenow": from_servicenow,
    "azuredevops": from_azuredevops,
    "github": from_github,
}
