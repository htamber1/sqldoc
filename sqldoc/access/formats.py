"""Render a generated access script into different execution formats, and run it
directly (with confirmation + audit) via `sqldoc access execute`.

Formats:
* **sql** — a plain ``.sql`` file (grant, with the rollback appended as comments);
* **powershell** — a self-contained PowerShell script wrapping ``Invoke-Sqlcmd``
  (with a ``-Rollback`` switch);
* **runbook** — an Azure Automation PowerShell runbook (uses an Automation
  credential + connection);
* **ansible** — an Ansible playbook using ``community.general.mssql_script``.
"""
import re

FORMATS = ("sql", "powershell", "runbook", "ansible")


def split_batches(sql: str) -> list:
    """Split a T-SQL script on GO batch separators (GO on its own line)."""
    parts = re.split(r"(?im)^\s*GO\s*;?\s*$", sql or "")
    return [p.strip() for p in parts if p.strip()]


# --- format renderers ------------------------------------------------------

def to_sql(gs) -> str:
    out = [gs.grant_sql.rstrip(), "", "-- ===== ROLLBACK (commented; uncomment to undo) ====="]
    for line in gs.rollback_sql.splitlines():
        out.append(line if line.startswith("--") else f"-- {line}")
    return "\n".join(out) + "\n"


def _ps_heredoc(sql: str) -> str:
    # Single-quoted here-string: literal, no interpolation. Closing '@ at column 0.
    return "@'\n" + (sql or "").replace("\r\n", "\n") + "\n'@"


def to_powershell(gs) -> str:
    return f"""<#
  sqldoc access grant — PowerShell / Invoke-Sqlcmd wrapper
  Server:   {gs.server}
  Database: {gs.database}
  Grantee:  {gs.login_name} ({gs.login_type})
  Run:      .\\grant.ps1            # apply
            .\\grant.ps1 -Rollback  # undo
  Requires the SqlServer module (Install-Module SqlServer).
#>
param(
    [string]$ServerInstance = "{gs.server}",
    [string]$Database = "{gs.database}",
    [switch]$Rollback
)

$grant = {_ps_heredoc(gs.grant_sql)}

$rollback = {_ps_heredoc(gs.rollback_sql)}

$query = if ($Rollback) {{ $rollback }} else {{ $grant }}
Write-Host "sqldoc: applying $(if ($Rollback) {{'ROLLBACK'}} else {{'grant'}}) to $ServerInstance / $Database"
Invoke-Sqlcmd -ServerInstance $ServerInstance -Database $Database -Query $query -TrustServerCertificate -ErrorAction Stop
Write-Host "sqldoc: done."
"""


def to_azure_runbook(gs) -> str:
    return f"""<#
  sqldoc access grant — Azure Automation runbook (PowerShell)
  Server:   {gs.server}
  Database: {gs.database}
  Grantee:  {gs.login_name} ({gs.login_type})
  Configure an Automation credential (default name 'SqlAdmin') with rights to
  manage security on the target database. Import the SqlServer module into the
  Automation account.
#>
param(
    [string]$ServerInstance = "{gs.server}",
    [string]$Database = "{gs.database}",
    [string]$CredentialName = "SqlAdmin",
    [switch]$Rollback
)

$cred = Get-AutomationPSCredential -Name $CredentialName

$grant = {_ps_heredoc(gs.grant_sql)}

$rollback = {_ps_heredoc(gs.rollback_sql)}

$query = if ($Rollback) {{ $rollback }} else {{ $grant }}
Write-Output "sqldoc runbook: applying $(if ($Rollback) {{'ROLLBACK'}} else {{'grant'}}) to $ServerInstance / $Database"
Invoke-Sqlcmd -ServerInstance $ServerInstance -Database $Database -Query $query ``
    -Credential $cred -TrustServerCertificate -ErrorAction Stop
Write-Output "sqldoc runbook: done."
"""


def _yaml_block(sql: str, indent: str) -> str:
    lines = (sql or "").rstrip().splitlines() or [""]
    return "\n".join(indent + ln for ln in lines)


def to_ansible(gs) -> str:
    ind = " " * 10
    return f"""---
# sqldoc access grant — Ansible playbook (community.general.mssql_script)
# Server:   {gs.server}
# Database: {gs.database}
# Grantee:  {gs.login_name} ({gs.login_type})
# Vars mssql_login_user / mssql_login_password must have rights to manage security.
# Run with -e "rollback=true" to undo.
- name: Grant SQL Server access ({gs.login_name} on {gs.database})
  hosts: localhost
  gather_facts: false
  vars:
    mssql_host: "{gs.server}"
    mssql_database: "{gs.database}"
    rollback: false
  tasks:
    - name: Apply grant
      when: not (rollback | bool)
      community.general.mssql_script:
        login_host: "{{{{ mssql_host }}}}"
        login_user: "{{{{ mssql_login_user }}}}"
        login_password: "{{{{ mssql_login_password }}}}"
        db: "{{{{ mssql_database }}}}"
        script: |
{_yaml_block(gs.grant_sql, ind)}
    - name: Roll back grant
      when: rollback | bool
      community.general.mssql_script:
        login_host: "{{{{ mssql_host }}}}"
        login_user: "{{{{ mssql_login_user }}}}"
        login_password: "{{{{ mssql_login_password }}}}"
        db: "{{{{ mssql_database }}}}"
        script: |
{_yaml_block(gs.rollback_sql, ind)}
"""


_RENDERERS = {
    "sql": to_sql,
    "powershell": to_powershell,
    "runbook": to_azure_runbook,
    "ansible": to_ansible,
}

_EXTENSIONS = {"sql": ".sql", "powershell": ".ps1", "runbook": ".ps1", "ansible": ".yml"}


def render_format(gs, fmt: str) -> str:
    if fmt not in _RENDERERS:
        raise ValueError(f"Unknown format '{fmt}' (choose from {', '.join(FORMATS)}).")
    return _RENDERERS[fmt](gs)


def extension_for(fmt: str) -> str:
    return _EXTENSIONS.get(fmt, ".txt")


# --- direct execution ------------------------------------------------------

def execute_batches(cursor, sql: str) -> int:
    """Execute each GO-separated batch on the cursor. Returns batches run."""
    batches = split_batches(sql)
    for batch in batches:
        cursor.execute(batch)
    return len(batches)
