"""Live validation of the publishing / ticketing integrations.

Every connector is mock-tested (SDK/transport factories injected). Each exposes a
``--test`` mode that verifies auth + connectivity to the real SaaS **without
touching a database**. Fill in the connector's section in the live config
(see ``tests/live/sqldoc.live.example.yml``) and this runs ``sqldoc <connector>
--test``, asserting a clean, authenticated connection.

To also validate a real **publish** (creates/updates real content in the target),
run manually, e.g.:
    sqldoc confluence --push --connection-string "<db>" --config tests/live/sqldoc.live.yml
"""
import pytest

from _liveutil import has_section, live_config_path, run

pytestmark = pytest.mark.live


# (CLI command, config-section key). Most match; onedrive reuses sharepoint auth
# but has its own section; the wikis/dropbox/nuclino use their own sections.
CONNECTORS = [
    ("sharepoint", "sharepoint"),
    ("confluence", "confluence"),
    ("notion", "notion"),
    ("gdrive", "gdrive"),
    ("box", "box"),
    ("jira", "jira"),
    ("servicenow", "servicenow"),
    ("azuredevops", "azuredevops"),
    ("powerbi", "powerbi"),
    ("webhook", "webhook"),
    ("github-wiki", "github_wiki"),
    ("gitlab-wiki", "gitlab_wiki"),
    ("azuredevops-wiki", "azuredevops_wiki"),
    ("onedrive", "onedrive"),
    ("dropbox", "dropbox"),
    ("nuclino", "nuclino"),
]


@pytest.mark.parametrize("command,section", CONNECTORS, ids=[c[0] for c in CONNECTORS])
def test_connector_authenticates(command, section):
    if not has_section(section):
        pytest.skip(f"add a '{section}:' section to {live_config_path()} "
                    f"to validate the {command} connector")
    r = run([command, "--test"])
    assert r.exit_code == 0, f"{command} --test failed:\n{r.output}"
    print(f"\n[{command}] --test OK: {r.output.strip().splitlines()[-1] if r.output.strip() else 'connected'}")
