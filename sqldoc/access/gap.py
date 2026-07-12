"""Compare a parsed access request against a user's current access.

Produces one of three verdicts — ALREADY / PARTIAL / NONE — with a plain-English
explanation, the access they already have, and the specific gap.
"""
from sqldoc.access.model import GapResult
from sqldoc.access.roles import roles_for_level, level_meets
from sqldoc.access.model import LEVEL_ORDER


def _db_access(report, database):
    dbl = (database or "").lower()
    return [a for a in report.access if (a.database or "").lower() == dbl]


def _max_level(access_rows):
    best = "none"
    for a in access_rows:
        if LEVEL_ORDER.get(a.level, 0) > LEVEL_ORDER.get(best, 0):
            best = a.level
    return best


def analyze_gap(parsed, report) -> GapResult:
    """Compare `parsed` (a ParsedRequest) against `report` (an AccessReport)."""
    needs = parsed.level or "read"
    rows = _db_access(report, parsed.database)
    have = _max_level(rows)

    current = []
    for a in rows:
        if a.roles:
            current.append(f"{a.database}: member of {', '.join(a.roles)} via {a.login}")
        elif a.permissions:
            current.append(f"{a.database}: {len(a.permissions)} explicit grant(s) via {a.login}")
    target_roles = roles_for_level(needs)

    if not rows or have == "none":
        return GapResult(
            verdict="NONE", request=parsed, have_level="none", needs_level=needs,
            explanation=(f"{report.user.display_name or report.user.identifier} has no access to "
                         f"'{parsed.database or '(unspecified database)'}'. To grant "
                         f"{needs} access, add a login/group to {', '.join(target_roles)}."),
            missing=[f"{needs} access to {parsed.database or '(database)'}"
                     + (f" (schema {parsed.schema})" if parsed.schema else "")]
                    + [f"membership in {r}" for r in target_roles],
            current=current)

    if level_meets(have, needs):
        role_note = ""
        for a in rows:
            if a.roles:
                role_note = f" (via {', '.join(a.roles)} on {a.database})"
                break
        return GapResult(
            verdict="ALREADY", request=parsed, have_level=have, needs_level=needs,
            explanation=(f"{report.user.display_name or report.user.identifier} already has "
                         f"{have} access to {parsed.database}{role_note}, which satisfies the "
                         f"requested {needs} access."),
            missing=[], current=current)

    # Has some access, but a lower level than requested.
    missing_roles = [r for r in target_roles
                     if not any(r in a.roles for a in rows)]
    return GapResult(
        verdict="PARTIAL", request=parsed, have_level=have, needs_level=needs,
        explanation=(f"{report.user.display_name or report.user.identifier} has {have} access to "
                     f"{parsed.database} but needs {needs}. Grant the missing role(s) to close "
                     f"the gap."),
        missing=[f"upgrade from {have} to {needs}"]
                + [f"membership in {r}" for r in missing_roles],
        current=current)
