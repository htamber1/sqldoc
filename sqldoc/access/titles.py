"""Heuristics tying job titles to an expected access level, and recognising
service accounts. Shared by the access review (over-privilege detection) and the
role recommender (least-privilege suggestion)."""
import re

# Title keyword -> the highest access level that title typically justifies.
_ADMIN_TITLES = ("dba", "database administrator", "administrator", "sysadmin",
                 "infrastructure", "platform engineer", "sre")
_WRITE_TITLES = ("engineer", "developer", "programmer", "etl", "data engineer",
                 "integration", "analyst engineer", "operations", "support engineer")
_READ_TITLES = ("analyst", "reporting", "report", "business intelligence", "bi ",
                "scientist", "auditor", "read", "viewer", "manager", "accountant",
                "finance", "sales", "marketing", "clerk", "specialist")

_LEVEL_RANK = {"none": 0, "read": 1, "write": 2, "admin": 3}


def expected_level_for_title(title: str) -> str:
    """The highest level a title usually justifies (default read — least privilege)."""
    t = (title or "").lower()
    if not t:
        return "read"
    if any(k in t for k in _ADMIN_TITLES):
        return "admin"
    if any(k in t for k in _WRITE_TITLES):
        return "write"
    return "read"


def exceeds(actual: str, expected: str, by: int = 1) -> bool:
    """True if `actual` is at least `by` ranks above `expected`."""
    return _LEVEL_RANK.get(actual, 0) - _LEVEL_RANK.get(expected, 0) >= by


_SERVICE_PATTERNS = [
    re.compile(r"(^|\\)svc[_\-]?", re.I),
    re.compile(r"(^|\\)service[_\-]?", re.I),
    re.compile(r"\$$"),                 # machine / gMSA account (name$)
    re.compile(r"(^|\\)sa_", re.I),
    re.compile(r"app[_\-]?pool", re.I),
    re.compile(r"[_\-](svc|service|daemon|agent|job)$", re.I),
]


def is_service_account(name: str, extra_patterns=None) -> bool:
    name = name or ""
    for pat in _SERVICE_PATTERNS:
        if pat.search(name):
            return True
    for p in (extra_patterns or []):
        if re.search(p, name, re.I):
            return True
    return False
