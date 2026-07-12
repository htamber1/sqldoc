"""Parse the ``access:`` section of ``.sqldoc.yml``.

Example::

    access:
      ad:
        type: auto            # ldap | graph | auto
        server: ldap://dc.corp.local
        base_dn: DC=corp,DC=local
        bind_dn: CN=svc,OU=Svc,DC=corp,DC=local
        bind_password: "***"
        netbios_domain: CORP
        # --- or Entra ID / Graph ---
        tenant_id: "..."
        client_id: "..."
        client_secret: "***"
      servers:
        - name: prod
          connection_string: "DRIVER={ODBC Driver 18 for SQL Server};SERVER=sql1;UID=sa;PWD=***"
          dialect: sqlserver
          databases: [Sales, HR]
      approvers:
        Sales: alice@corp.com
        default: dba@corp.com
      review:
        inactive_days: 90
"""


def section(cfg: dict) -> dict:
    raw = (cfg or {}).get("access")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("The 'access:' config section must be a mapping.")
    return raw


def ad_config(cfg: dict) -> dict:
    return section(cfg).get("ad") or {}


def servers(cfg: dict) -> list:
    raw = section(cfg).get("servers") or []
    if not isinstance(raw, list):
        raise ValueError("access.servers must be a list of server entries.")
    out = []
    for s in raw:
        if not isinstance(s, dict):
            raise ValueError("Each access.servers entry must be a mapping.")
        dbs = s.get("databases") or []
        if isinstance(dbs, str):
            dbs = [dbs]
        out.append({
            "name": s.get("name") or s.get("server") or "server",
            "connection_string": s.get("connection_string"),
            "server": s.get("server"), "username": s.get("username"),
            "password": s.get("password"), "dialect": s.get("dialect", "sqlserver"),
            "databases": list(dbs),
        })
    return out


def approvers(cfg: dict) -> dict:
    return section(cfg).get("approvers") or {}


def review_config(cfg: dict) -> dict:
    return section(cfg).get("review") or {}
