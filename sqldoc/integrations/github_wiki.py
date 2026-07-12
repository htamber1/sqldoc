"""GitHub project Wiki publisher.

GitHub wikis are plain git repositories (``<repo>.wiki.git``) with no REST API,
so this connector clones the wiki, writes one Markdown page per database, commits
and pushes. Requires ``git`` on PATH and a token with repo scope.

Config (``github_wiki:``)::

    github_wiki:
      repo: owner/name
      token: "***"
      author_name: sqldoc bot
      author_email: bot@acme.com
      host: github.com            # or GitHub Enterprise host
"""
import os
import tempfile

from sqldoc.integrations.base import IntegrationError, need, result
from sqldoc.integrations.reports import bundle_markdown


def run_git(args, cwd=None):
    """Run a git command (module-level for mocking)."""
    import subprocess
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise IntegrationError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _wiki_url(cfg) -> str:
    host = cfg.get("host", "github.com")
    token = cfg.get("token", "")
    auth = f"{token}@" if token else ""
    return f"https://{auth}{host}/{cfg['repo']}.wiki.git"


def _page_name(database: str) -> str:
    # GitHub wiki turns spaces into hyphens; keep it simple + filesystem-safe.
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in database) or "Database"


class Client:
    def __init__(self, config):
        self.cfg = config or {}

    def test(self):
        need(self.cfg, "repo", "token", integration="github_wiki")
        run_git(["ls-remote", _wiki_url(self.cfg)])
        return result(True, f"Wiki repo for '{self.cfg['repo']}' reachable.")

    def push_reports(self, artifacts, metrics=None, bundle=None):
        need(self.cfg, "repo", "token", integration="github_wiki")
        if bundle is None:
            raise IntegrationError("GitHub Wiki push needs the collected bundle.")
        page = _page_name(bundle.database)
        content = bundle_markdown(bundle, metrics)
        url = _wiki_url(self.cfg)
        with tempfile.TemporaryDirectory(prefix="sqldoc-wiki-") as tmp:
            run_git(["clone", "--depth", "1", url, tmp])
            with open(os.path.join(tmp, f"{page}.md"), "w", encoding="utf-8") as f:
                f.write(content)
            run_git(["add", "-A"], cwd=tmp)
            name = self.cfg.get("author_name", "sqldoc")
            email = self.cfg.get("author_email", "sqldoc@localhost")
            run_git(["-c", f"user.name={name}", "-c", f"user.email={email}",
                     "commit", "-m", f"sqldoc: update {bundle.database} documentation"], cwd=tmp)
            run_git(["push", "origin", "HEAD"], cwd=tmp)
        return result(True, f"Published GitHub wiki page '{page}'.", page=page)
