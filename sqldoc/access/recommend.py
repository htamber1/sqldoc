"""Recommend least-privilege database roles from job title + department.

Learns from existing users: scans the configured databases for principals that
map to AD users, groups them by department + title, and recommends the roles
that peers with the same role profile hold — capped at the level the title
justifies (least privilege). AI writes the rationale; a deterministic model is
the fallback so it works offline.
"""
from collections import Counter
from dataclasses import dataclass, field

from sqldoc.dbutil import cell
from sqldoc.access.model import RoleRecommendation, LEVEL_ORDER
from sqldoc.access.roles import roles_for_level
from sqldoc.access.sqlserver import (
    ROLE_LEVEL, _name_part, DB_PRINCIPALS_SQL, DB_ROLE_MEMBERS_SQL)
from sqldoc.access.titles import expected_level_for_title


@dataclass
class PeerProfile:
    login: str
    title: str = ""
    department: str = ""
    database: str = ""
    roles: list = field(default_factory=list)


def gather_peers(cfg, source, adapter_factory=None) -> list:
    """Existing users (resolved in AD) and the roles they hold, across the
    configured databases — the training data for recommendations."""
    from sqldoc.access import config as access_config
    from sqldoc.access.checker import build_db_adapter
    factory = adapter_factory or build_db_adapter
    peers = []
    for entry in access_config.servers(cfg):
        for database in entry["databases"]:
            try:
                adapter = factory(entry, database)
                conn = adapter.connect()
                try:
                    cursor = adapter.cursor(conn)
                    cursor.execute(DB_PRINCIPALS_SQL)
                    principals = {cell(r, "db_user"): cell(r, "type_desc")
                                  for r in cursor.fetchall()}
                    cursor.execute(DB_ROLE_MEMBERS_SQL)
                    roles_by = {}
                    for r in cursor.fetchall():
                        roles_by.setdefault(cell(r, "member_name"), []).append(cell(r, "role_name"))
                    for member, ptype in principals.items():
                        if source is None or "\\" not in member or "USER" not in (ptype or "").upper():
                            continue
                        try:
                            u = source.get_user(_name_part(member))
                        except Exception:
                            u = None
                        if u is not None and u.found and (u.title or u.department):
                            peers.append(PeerProfile(login=member, title=u.title,
                                                     department=u.department, database=database,
                                                     roles=sorted(roles_by.get(member, []))))
                finally:
                    conn.close()
            except Exception:
                continue
    return peers


def recommend_roles(user, peers, database=None, mode="local", model=None,
                    backend=None, no_ai=False) -> RoleRecommendation:
    """Recommend least-privilege roles for `user` from the peer population."""
    expected = expected_level_for_title(user.title)
    dept = (user.department or "").lower()
    matching = [p for p in peers
                if (not database or (p.database or "").lower() == database.lower())
                and (p.department or "").lower() == dept
                and expected_level_for_title(p.title) == expected]
    n = len(matching)

    counts = Counter()
    for p in matching:
        for r in set(p.roles):
            counts[r] += 1

    recommended, seen = [], set()
    for r in roles_for_level(expected):
        recommended.append((r, f"least-privilege baseline for '{user.title or 'user'}'"))
        seen.add(r)
    for r, c in counts.most_common():
        if r in seen:
            continue
        # Cap at the title's level — never recommend above what it justifies.
        if LEVEL_ORDER.get(ROLE_LEVEL.get(r, "read"), 0) > LEVEL_ORDER.get(expected, 0):
            continue
        if n and c / n >= 0.5:
            recommended.append((r, f"held by {c}/{n} peer(s) in {user.department or 'the department'}"))
            seen.add(r)

    lp_note = (f"Capped at '{expected}' — the level a '{user.title or 'user'}' typically "
               f"justifies. Roles peers hold above that level were excluded (least privilege).")
    rationale = _rationale(user, expected, n, recommended, mode, model, backend, no_ai)
    return RoleRecommendation(
        user=user, database=database or "", recommended_roles=recommended,
        peers_considered=n, rationale=rationale, least_privilege_note=lp_note)


def _rationale(user, expected, n, recommended, mode, model, backend, no_ai) -> str:
    base = (f"Based on {n} peer(s) with a similar role in "
            f"{user.department or 'the department'}, recommend {expected} access via "
            f"{', '.join(r for r, _ in recommended)}.")
    if no_ai or n == 0:
        return base
    try:
        from sqldoc import ai
        prompt = (
            "In one or two sentences, justify (for a DBA) granting these SQL Server "
            f"roles to a '{user.title}' in '{user.department}': "
            f"{', '.join(r for r, _ in recommended)}. Emphasise least privilege. "
            f"There are {n} similar existing users.")
        text = ai.dispatch(prompt, mode=mode, model=model, backend=backend, max_tokens=150).strip()
        return text or base
    except Exception:
        return base
