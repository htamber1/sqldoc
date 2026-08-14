"""Parse the ``access:`` section of ``.sqldoc.yml``.

Example::

    access:
      ad:
        type: auto            # ldap | graph | auto
        server: ldap://dc.corp.local
        base_dn: DC=corp,DC=local
        netbios_domain: CORP
        # Passwordless: bind over SASL/GSSAPI as the current Windows user
        # (needs winkerberos on Windows, gssapi elsewhere). Inherited from the
        # top-level `windows_auth:` unless a bind_dn is set below.
        windows_auth: true
        # ...or a stored service account instead of windows_auth:
        # bind_dn: CN=svc,OU=Svc,DC=corp,DC=local
        # bind_password: "***"
        # --- or Entra ID / Graph ---
        tenant_id: "..."
        client_id: "..."
        client_secret: "***"
      servers:
        # Preferred: discrete parts. sqldoc builds the connection string with
        # the adapter for `dialect`, and the ODBC driver comes from the
        # top-level `driver:` key (or a per-entry `driver:`), so nothing here
        # is pinned to one driver version.
        - name: prod
          server: sql1
          username: sa
          password: "***"
          dialect: sqlserver
          databases: [Sales, HR]
        # Windows auth needs no username/password. `windows_auth:` and
        # `driver:` are inherited from the top level when omitted here, so a
        # Windows-auth shop can just set `windows_auth: true` once globally.
        - name: warehouse
          server: sql2
          windows_auth: true
          dialect: sqlserver
          databases: [Ledger]
        # Or supply a ready-made connection string, which is used verbatim:
        - name: analytics
          connection_string: "postgresql://readonly:***@pg1/analytics"
          dialect: postgres
          databases: [public]
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
    raw = section(cfg).get("ad") or {}
    if not raw:
        return {}
    ad = dict(raw)
    # Per-section Windows auth, else the top-level `windows_auth:`. Inheriting is
    # skipped when an explicit bind account is configured, so a global
    # `windows_auth: true` (meant for SQL Server) never silently discards a
    # bind_dn/bind_password the user set for the directory.
    wa = ad.get("windows_auth")
    if wa is None and not ad.get("bind_dn"):
        wa = (cfg or {}).get("windows_auth")
    if wa is not None:
        ad["windows_auth"] = bool(wa)
    return ad


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
        # Per-entry Windows auth, else the top-level `windows_auth:` setting --
        # the same inheritance the rest of sqldoc uses. Tested against None (not
        # falsiness) so an explicit per-entry `windows_auth: false` still wins
        # over a top-level true.
        wa = s.get("windows_auth")
        if wa is None:
            wa = (cfg or {}).get("windows_auth")
        out.append({
            "name": s.get("name") or s.get("server") or "server",
            "connection_string": s.get("connection_string"),
            "server": s.get("server"), "username": s.get("username"),
            "password": s.get("password"), "dialect": s.get("dialect", "sqlserver"),
            "databases": list(dbs),
            # Per-entry ODBC driver, else the top-level `driver:` override.
            "driver": s.get("driver") or (cfg or {}).get("driver"),
            "windows_auth": bool(wa),
        })
    return out


def approvers(cfg: dict) -> dict:
    return section(cfg).get("approvers") or {}


def review_config(cfg: dict) -> dict:
    return section(cfg).get("review") or {}
