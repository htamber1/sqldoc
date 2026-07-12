"""The sqldoc integration suite.

Ships report/metric/finding push connectors for the systems teams actually keep
their documentation and tickets in — SharePoint, Confluence, Notion, Google
Drive, Box, Jira, ServiceNow, Azure DevOps, Power BI — plus a generic
configurable webhook and enterprise notification channels (Teams, Webex,
PagerDuty, OpsGenie).

Each connector lives in its own module and exposes a ``Client(config)`` class
with a ``test()`` method and whichever of ``push_reports`` / ``push_metrics`` /
``create_issues`` / ``notify`` fits it. All third-party SDKs are optional extras
(``pip install sqldoc[confluence]`` etc.); nothing here is imported at
``import sqldoc`` time.
"""
from sqldoc.integrations.base import (
    Artifact, FindingEvent, IntegrationError, need, require, result,
)
from sqldoc.integrations.config import SECTIONS, section, is_configured

# name -> (module suffix, pip extra) for the report/metric/issue connectors that
# expose a CLI command. Alerting channels (teams/webex/pagerduty/opsgenie) are
# wired through the agent notifier, not here.
_MODULES = {
    "sharepoint": ("sharepoint", "sharepoint"),
    "confluence": ("confluence", "confluence"),
    "notion": ("notion", "notion"),
    "gdrive": ("gdrive", "gdrive"),
    "box": ("box", "box"),
    "jira": ("jira", "jira"),
    "servicenow": ("servicenow", "servicenow"),
    "azuredevops": ("azuredevops", "azuredevops"),
    "powerbi": ("powerbi", "powerbi"),
    "webhook": ("webhook", ""),
}


def get_client(name: str, config: dict):
    """Instantiate an integration's ``Client`` from its config mapping."""
    import importlib
    if name not in _MODULES:
        raise IntegrationError(f"Unknown integration '{name}'.")
    mod = importlib.import_module(f"sqldoc.integrations.{_MODULES[name][0]}")
    return mod.Client(config)


__all__ = [
    "Artifact", "FindingEvent", "IntegrationError", "need", "require", "result",
    "SECTIONS", "section", "is_configured", "get_client",
]
