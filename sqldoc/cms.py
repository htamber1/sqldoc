"""Central Management Server (CMS) support.

A SQL Server CMS stores a shared inventory of *registered servers* organised into
*server groups* in its ``msdb`` database. sqldoc reads that inventory so every
command can fan out across the whole estate:

* ``discover_inventory`` reads the two shared tables into a :class:`CmsInventory`
  (a group tree + registered servers with computed group paths);
* ``connection_string_for`` builds a per-server connection string (Windows auth by
  default — CMS registrations don't store credentials);
* ``servers_in_group`` filters to one group (traversing nested subgroups);
* ``to_config`` / ``from_config`` round-trip the inventory through the
  ``cms_servers:`` section of ``.sqldoc.yml`` so other commands run offline
  against the saved inventory.

Read-only: only catalog tables are queried, never row data.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqldoc.dbutil import cell

# The built-in root group. Its name is an implementation detail we hide from paths.
SYSTEM_ROOT = "DatabaseEngineServerGroup"

GROUPS_SQL = """
    /* CMS_GROUPS */
    SELECT server_group_id, name, parent_id, description, is_system_object
    FROM msdb.dbo.sysmanagement_shared_server_groups
    ORDER BY server_group_id
"""

SERVERS_SQL = """
    /* CMS_SERVERS */
    SELECT server_id, server_group_id, name, server_name, description
    FROM msdb.dbo.sysmanagement_shared_registered_servers
    ORDER BY name
"""


@dataclass
class CmsGroup:
    id: int
    name: str
    parent_id: object = None       # int or None for the root
    description: str = ""
    is_system: bool = False
    path: str = ""                 # e.g. "Production/West" (system root omitted)


@dataclass
class CmsServer:
    name: str                      # the registered display name
    server_name: str               # the actual host/instance to connect to
    group_id: object = None
    group_path: str = ""
    description: str = ""


@dataclass
class CmsInventory:
    cms_server: str = ""
    groups: list = field(default_factory=list)
    servers: list = field(default_factory=list)
    discovered_at: str = ""

    def group_by_id(self):
        return {g.id: g for g in self.groups}


# --- discovery -------------------------------------------------------------

def _compute_path(group_id, by_id) -> str:
    parts, seen, gid = [], set(), group_id
    while gid is not None and gid in by_id and gid not in seen:
        seen.add(gid)
        g = by_id[gid]
        if not g.is_system:
            parts.append(g.name)
        gid = g.parent_id
    return "/".join(reversed(parts))


def discover_inventory(cursor, cms_server: str = "") -> CmsInventory:
    """Read the CMS shared inventory from a cursor on the CMS server's msdb."""
    cursor.execute(GROUPS_SQL)
    groups = []
    for r in cursor.fetchall():
        pid = cell(r, "parent_id")
        groups.append(CmsGroup(
            id=int(cell(r, "server_group_id")),
            name=cell(r, "name"),
            parent_id=(int(pid) if pid not in (None, "") else None),
            description=cell(r, "description") or "",
            is_system=bool(int(cell(r, "is_system_object") or 0))))
    by_id = {g.id: g for g in groups}
    for g in groups:
        g.path = _compute_path(g.id, by_id)

    cursor.execute(SERVERS_SQL)
    servers = []
    for r in cursor.fetchall():
        gid = cell(r, "server_group_id")
        gid = int(gid) if gid not in (None, "") else None
        servers.append(CmsServer(
            name=cell(r, "name"),
            server_name=cell(r, "server_name") or cell(r, "name"),
            group_id=gid,
            group_path=by_id[gid].path if gid in by_id else "",
            description=cell(r, "description") or ""))

    return CmsInventory(
        cms_server=cms_server, groups=groups, servers=servers,
        discovered_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())


def connect_cms(cms_server: str, windows_auth: bool = True, username: str = None,
                password: str = None):
    """Open a connection to the CMS server's msdb (module-level for mocking)."""
    import pyodbc
    return pyodbc.connect(connection_string_for(cms_server, database="msdb",
                                                windows_auth=windows_auth,
                                                username=username, password=password),
                          timeout=15)


def discover_live(cms_server, windows_auth=True, username=None, password=None) -> CmsInventory:
    conn = connect_cms(cms_server, windows_auth, username, password)
    try:
        return discover_inventory(conn.cursor(), cms_server)
    finally:
        conn.close()


# --- connection building ---------------------------------------------------

def connection_string_for(server_name: str, database: str = "master",
                          windows_auth: bool = True, username: str = None,
                          password: str = None) -> str:
    base = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server_name};"
            f"DATABASE={database};TrustServerCertificate=yes;")
    if windows_auth or not username:
        return base + "Trusted_Connection=yes;"
    return base + f"UID={username};PWD={password};"


# --- group filtering (nested) ----------------------------------------------

def _descendant_group_ids(inventory: CmsInventory, root_id) -> set:
    by_parent = {}
    for g in inventory.groups:
        by_parent.setdefault(g.parent_id, []).append(g.id)
    out, stack = set(), [root_id]
    while stack:
        gid = stack.pop()
        if gid in out:
            continue
        out.add(gid)
        stack.extend(by_parent.get(gid, []))
    return out


def find_groups(inventory: CmsInventory, group: str) -> list:
    """Groups matching `group` by full path or by leaf name (case-insensitive)."""
    g = (group or "").strip().strip("/").lower()
    matches = []
    for grp in inventory.groups:
        if grp.is_system:
            continue
        if grp.path.lower() == g or grp.name.lower() == g:
            matches.append(grp)
    return matches


def servers_in_group(inventory: CmsInventory, group: str, recursive: bool = True) -> list:
    """Servers in a named group (traversing nested subgroups by default)."""
    matches = find_groups(inventory, group)
    if not matches:
        return []
    ids = set()
    for grp in matches:
        ids |= _descendant_group_ids(inventory, grp.id) if recursive else {grp.id}
    return [s for s in inventory.servers if s.group_id in ids]


def select_servers(inventory: CmsInventory, group: str = None, recursive: bool = True) -> list:
    return servers_in_group(inventory, group, recursive) if group else list(inventory.servers)


# --- config round-trip -----------------------------------------------------

def to_config(inventory: CmsInventory) -> dict:
    return {
        "cms": inventory.cms_server,
        "discovered_at": inventory.discovered_at,
        "groups": [{"id": g.id, "name": g.name, "parent_id": g.parent_id,
                    "path": g.path, "description": g.description, "is_system": g.is_system}
                   for g in inventory.groups],
        "servers": [{"name": s.name, "server_name": s.server_name, "group_id": s.group_id,
                     "group_path": s.group_path, "description": s.description}
                    for s in inventory.servers],
    }


def from_config(cfg: dict) -> CmsInventory:
    """Reconstruct the inventory from the `cms_servers:` section of a loaded config."""
    raw = (cfg or {}).get("cms_servers") or {}
    groups = [CmsGroup(id=g.get("id"), name=g.get("name", ""), parent_id=g.get("parent_id"),
                       description=g.get("description", ""), is_system=g.get("is_system", False),
                       path=g.get("path", "")) for g in raw.get("groups", [])]
    servers = [CmsServer(name=s.get("name", ""), server_name=s.get("server_name", ""),
                         group_id=s.get("group_id"), group_path=s.get("group_path", ""),
                         description=s.get("description", "")) for s in raw.get("servers", [])]
    return CmsInventory(cms_server=raw.get("cms", ""), groups=groups, servers=servers,
                        discovered_at=raw.get("discovered_at", ""))


def has_inventory(cfg: dict) -> bool:
    return bool((cfg or {}).get("cms_servers", {}).get("servers"))


def save_cms_servers(config_path: str, inventory: CmsInventory):
    """Merge the discovered inventory into .sqldoc.yml under `cms_servers:`.
    Round-trips via YAML (comments in the file are not preserved)."""
    import os
    import yaml
    data = {}
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data["cms_servers"] = to_config(inventory)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
