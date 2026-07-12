"""Parse a plain-English access request into structured intent.

"read access to the Sales database" -> database=Sales, schema="", level=read.
"write access to the HR schema"      -> schema=HR, level=write.

An AI backend (via ``ai.dispatch``) does the parsing; a deterministic heuristic
is the fallback (and the ``--no-ai`` path), so the command still works offline.
"""
import json
import re

from sqldoc.access.model import ParsedRequest

_WRITE_WORDS = ("write", "insert", "update", "delete", "modify", "edit", "change", "load")
_ADMIN_WORDS = ("admin", "owner", "full control", "ddl", "manage", "alter", "create table",
                "schema change", "db_owner")


def heuristic_parse(text: str, known_databases=None) -> ParsedRequest:
    """Regex/keyword parse — no AI. Deterministic and always available."""
    low = (text or "").lower()
    level = "read"
    if any(w in low for w in _ADMIN_WORDS):
        level = "admin"
    elif any(w in low for w in _WRITE_WORDS):
        level = "write"

    database = ""
    for db in (known_databases or []):
        if db and re.search(rf"\b{re.escape(db.lower())}\b", low):
            database = db
            break
    if not database:
        m = re.search(r"(?:to|on|in|for)\s+(?:the\s+)?([A-Za-z0-9_]+)\s+database", low)
        if not m:
            m = re.search(r"database\s+(?:called\s+|named\s+)?([A-Za-z0-9_]+)", low)
        if m:
            database = m.group(1)

    schema = ""
    m = re.search(r"([A-Za-z0-9_]+)\s+schema", low)
    if not m:
        m = re.search(r"schema\s+([A-Za-z0-9_]+)", low)
    if m:
        schema = m.group(1)

    return ParsedRequest(raw=text, database=database, schema=schema, level=level,
                         confidence=0.5 if database else 0.2, note="heuristic parse")


def _extract_json(s: str) -> dict:
    m = re.search(r"\{.*\}", s or "", re.DOTALL)
    if not m:
        raise ValueError("no JSON object in AI response")
    return json.loads(m.group(0))


def _normalize_level(v) -> str:
    v = str(v or "").strip().lower()
    if v in ("admin", "owner", "control", "ddl"):
        return "admin"
    if v in ("write", "readwrite", "read-write", "read_write", "modify", "insert", "update", "delete"):
        return "write"
    return "read"


def parse_request(text: str, known_databases=None, mode="local", model=None,
                  backend=None, no_ai=False) -> ParsedRequest:
    """Parse a request, preferring AI and falling back to the heuristic."""
    if no_ai:
        return heuristic_parse(text, known_databases)

    dbs = ", ".join(known_databases or []) or "(unknown)"
    prompt = (
        "You extract a database access request into JSON. "
        "Return ONLY a JSON object with keys: database (string), schema (string, "
        "empty if the whole database), level (one of read, write, admin), objects "
        "(array of specific table names, usually empty). "
        f"Known databases: {dbs}. "
        f'Request: "{text}"\n'
        'Example: {"database":"Sales","schema":"","level":"read","objects":[]}')
    try:
        from sqldoc import ai
        raw = ai.dispatch(prompt, mode=mode, model=model, backend=backend, max_tokens=200)
        data = _extract_json(raw)
        parsed = ParsedRequest(
            raw=text,
            database=str(data.get("database") or "").strip(),
            schema=str(data.get("schema") or "").strip(),
            level=_normalize_level(data.get("level")),
            objects=[str(o) for o in (data.get("objects") or [])],
            confidence=0.85, note="AI parse")
        # If the AI missed the database but the heuristic can find it, backfill.
        if not parsed.database:
            h = heuristic_parse(text, known_databases)
            parsed.database = h.database
            if not parsed.schema:
                parsed.schema = h.schema
        return parsed
    except Exception as e:
        h = heuristic_parse(text, known_databases)
        h.note = f"heuristic fallback (AI unavailable: {type(e).__name__})"
        return h
