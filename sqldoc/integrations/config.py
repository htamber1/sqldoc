"""Read per-integration config from the loaded ``.sqldoc.yml``.

Each integration is configured under its own top-level key, e.g.::

    confluence:
      base_url: https://acme.atlassian.net/wiki
      email: bot@acme.com
      api_token: "***"
      space_key: DBDOCS
      parent_page_id: "123456"

    sharepoint:
      tenant_id: "..."
      client_id: "..."
      client_secret: "***"
      site_id: "acme.sharepoint.com,<siteGuid>,<webGuid>"
      list_name: "Database Documentation"

The agent's ``integrations:`` list under ``agent:`` names which of these to
auto-push every ``push_interval_hours`` (default 24).
"""

# Every integration that owns a top-level config section. Also drives CONFIG_KEYS
# in the CLI so an unknown-key check doesn't reject a valid integration section.
SECTIONS = (
    "sharepoint", "confluence", "notion", "gdrive", "box",
    "jira", "servicenow", "azuredevops", "powerbi",
    "webhook", "webex",
    "github_wiki", "gitlab_wiki", "azuredevops_wiki", "onedrive", "dropbox", "nuclino",
)


def section(cfg: dict, name: str) -> dict:
    """Return the config mapping for one integration (or {} if absent). Raises
    ValueError if the section is present but not a mapping."""
    raw = (cfg or {}).get(name)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"The '{name}:' config section must be a mapping.")
    return raw


def is_configured(cfg: dict, name: str) -> bool:
    return bool(section(cfg, name))
