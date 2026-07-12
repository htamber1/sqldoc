"""Mapping between coarse access levels and SQL Server fixed database roles.

Used by the request/gap analysis (what role is missing) and the script generator
(what to add the login to), following the least-privilege convention: a writer is
also a reader.
"""
from sqldoc.access.model import LEVEL_ORDER

LEVEL_ROLES = {
    "read": ["db_datareader"],
    "write": ["db_datareader", "db_datawriter"],
    "admin": ["db_owner"],
}


def roles_for_level(level: str) -> list:
    return LEVEL_ROLES.get(level, ["db_datareader"])


def level_meets(have: str, needs: str) -> bool:
    return LEVEL_ORDER.get(have, 0) >= LEVEL_ORDER.get(needs, 0)
