"""End-to-end Jira access-request processing.

Pulls a ticket via the existing Jira connector, uses AI to extract who needs
access + what database/level + business justification, runs the full check +
script-generation workflow, and posts the generated script + impact analysis +
execution instructions back as a comment (optionally transitioning the ticket).
"""
import json
import re
from dataclasses import dataclass, field

from sqldoc.access.model import ParsedRequest


@dataclass
class TicketRequest:
    user: str = ""
    database: str = ""
    level: str = "read"
    justification: str = ""
    note: str = ""


@dataclass
class TicketResult:
    ticket: str = ""
    summary: str = ""
    extracted: TicketRequest = None
    report: object = None
    parsed: ParsedRequest = None
    gap: object = None
    script: object = None
    comment_posted: bool = False
    transitioned: bool = False
    note: str = ""


def _extract_json(s: str) -> dict:
    m = re.search(r"\{.*\}", s or "", re.DOTALL)
    if not m:
        raise ValueError("no JSON in AI response")
    return json.loads(m.group(0))


def extract_request(summary: str, description: str, known_databases=None,
                    mode="local", model=None, backend=None, no_ai=False) -> TicketRequest:
    """Pull the access request out of the ticket text (AI, with a heuristic fallback)."""
    text = f"{summary}\n{description}".strip()
    if not no_ai:
        prompt = (
            "Extract the database access request from this Jira ticket. Return ONLY "
            "a JSON object with keys: user (the username or email who needs access), "
            "database, level (read, write, or admin), justification (short). "
            f"Known databases: {', '.join(known_databases or []) or '(unknown)'}. "
            f"Ticket:\n{text}")
        try:
            from sqldoc import ai
            data = _extract_json(ai.dispatch(prompt, mode=mode, model=model,
                                             backend=backend, max_tokens=300))
            level = str(data.get("level", "read")).lower()
            level = level if level in ("read", "write", "admin") else "read"
            return TicketRequest(
                user=str(data.get("user") or "").strip(),
                database=str(data.get("database") or "").strip(),
                level=level, justification=str(data.get("justification") or "").strip(),
                note="AI extraction")
        except Exception as e:
            note = f"heuristic fallback (AI unavailable: {type(e).__name__})"
    else:
        note = "heuristic extraction"

    from sqldoc.access.parse import heuristic_parse
    p = heuristic_parse(text, known_databases)
    user = ""
    m = re.search(r"[\w.\-]+@[\w.\-]+", text)         # email
    if m:
        user = m.group(0)
    else:
        m = re.search(r"(?:for|user|grant)\s+([A-Za-z][\w.\\\-]+)", text, re.IGNORECASE)
        if m:
            user = m.group(1)
    return TicketRequest(user=user, database=p.database, level=p.level, note=note)


def _instructions(gs, extracted) -> str:
    if not gs or not gs.grant_sql.strip() or "No changes" in (gs.note or ""):
        return ("No script required — the grantee already has the requested access.")
    return (f"Run the grant script on server '{gs.server}', database '{gs.database}', "
            f"as a member of a role able to manage security (e.g. db_securityadmin or "
            f"sysadmin). A rollback script is included to undo the change. "
            f"Grantee: {gs.login_name} "
            f"({'Windows group' if gs.uses_windows_group else 'individual login'}).")


def build_comment_blocks(result: TicketResult):
    """ADF blocks summarising the workflow outcome for the Jira comment."""
    gap = result.gap
    gs = result.script
    blocks = [("h", "sqldoc access request analysis")]
    ex = result.extracted
    blocks.append(("p", f"Request: {ex.level} access to {ex.database or '(unspecified)'} "
                        f"for {ex.user or '(unknown user)'}."
                        + (f" Justification: {ex.justification}" if ex.justification else "")))
    if gap is not None:
        blocks.append(("p", f"Verdict: {gap.verdict}. {gap.explanation}"))
    if gs is not None and gs.grant_sql.strip() and "No changes" not in (gs.note or ""):
        blocks.append(("h", "Grant script"))
        blocks.append(("code", gs.grant_sql))
        blocks.append(("h", "Rollback script"))
        blocks.append(("code", gs.rollback_sql))
        if gs.pii_exposed:
            pii = ", ".join(f"{s}.{t} ({r})" for (s, t, r, _g) in gs.pii_exposed)
            blocks.append(("p", f"PII exposure: {len(gs.pii_exposed)} table(s) become accessible: {pii}"))
    blocks.append(("h", "Execution instructions"))
    blocks.append(("p", _instructions(gs, ex)))
    blocks.append(("p", "Generated automatically by sqldoc."))
    return blocks


def process_ticket(cfg, ticket, jira_client, post_comment=True, transition_to=None,
                   user_override=None, mode="local", model=None, backend=None,
                   no_ai=False) -> TicketResult:
    """Run the full workflow for one Jira ticket."""
    from sqldoc.integrations.jira import adf_to_text, adf_from_blocks
    from sqldoc.access import config as access_config
    from sqldoc.access.checker import check_access
    from sqldoc.access.parse import parse_request
    from sqldoc.access.gap import analyze_gap
    from sqldoc.access.script import generate_script

    issue = jira_client.get_issue(ticket)
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")
    description = adf_to_text(fields.get("description"))
    result = TicketResult(ticket=ticket, summary=summary)

    known = [db for s in access_config.servers(cfg) for db in s["databases"]]
    extracted = extract_request(summary, description, known_databases=known,
                                mode=mode, model=model, backend=backend, no_ai=no_ai)
    if user_override:
        extracted.user = user_override
    result.extracted = extracted

    if not extracted.user:
        result.note = ("Could not determine who needs access from the ticket; "
                       "re-run with --user.")
        if post_comment:
            result.comment_posted = _safe_comment(
                jira_client, ticket,
                adf_from_blocks([("p", result.note + " (sqldoc)")]))
        return result

    report = check_access(cfg, extracted.user)
    parsed = parse_request(f"{extracted.level} access to {extracted.database}",
                           known_databases=known, no_ai=True)
    parsed.database = extracted.database or parsed.database
    parsed.level = extracted.level
    gap = analyze_gap(parsed, report)
    result.report, result.parsed, result.gap = report, parsed, gap

    # Impact analysis needs the target database's tables.
    tables, pii, server_name = _tables_for(cfg, parsed.database)
    result.script = generate_script(report, parsed, server_name or "(server)", parsed.database,
                                    tables=tables, pii_findings=pii)

    if post_comment:
        result.comment_posted = _safe_comment(
            jira_client, ticket, adf_from_blocks(build_comment_blocks(result)))
    if transition_to:
        try:
            result.transitioned = jira_client.transition(ticket, transition_to)
        except Exception:
            result.transitioned = False
    return result


def _safe_comment(jira_client, ticket, adf) -> bool:
    try:
        jira_client.add_comment(ticket, adf)
        return True
    except Exception:
        return False


def _tables_for(cfg, database):
    from sqldoc.access import config as access_config
    from sqldoc.access.checker import build_db_adapter
    from sqldoc.pii import scan_tables
    for entry in access_config.servers(cfg):
        if any(db.lower() == (database or "").lower() for db in entry["databases"]):
            try:
                adapter = build_db_adapter(entry, database)
                tables = adapter.extract_metadata()
                return tables, scan_tables(tables), entry["name"]
            except Exception:
                return [], [], entry["name"]
    return [], [], ""
