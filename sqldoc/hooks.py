"""Git pre-commit hook installer.

`sqldoc install-hooks` drops a pre-commit hook into a repo's `.git/hooks` that
scans staged `.sql` files for HIGH-risk PII columns (via `sqldoc scan-files
--fail-on high`) and blocks the commit if any are found. This shifts
regulated-data review left — a developer who stages a migration adding an `ssn`
or `credit_card` column is stopped before the schema ever reaches a server.

The hook is a small POSIX-sh script so it runs on Linux/macOS and under Git for
Windows' bundled bash (which is what `core.hooksPath`/Git invokes on Windows).
"""
import os
import stat
import subprocess


HOOK_MARKER = "# >>> sqldoc pre-commit hook >>>"

HOOK_SCRIPT = """#!/bin/sh
{marker}
# Installed by `sqldoc install-hooks`. Scans staged .sql files for HIGH-risk PII
# columns and blocks the commit if any are found. Remove this file (or run
# `git commit --no-verify`) to bypass.
staged=$(git diff --cached --name-only --diff-filter=ACM | grep -iE '\\.sql$' || true)
if [ -z "$staged" ]; then
    exit 0
fi
echo "sqldoc: scanning staged SQL files for HIGH-risk PII..."
# shellcheck disable=SC2086
sqldoc scan-files --fail-on high $staged
status=$?
if [ $status -ne 0 ]; then
    echo ""
    echo "sqldoc: commit blocked — HIGH-risk PII columns detected in staged SQL."
    echo "        Review the findings above, add an allowlist entry in .sqldoc.yml,"
    echo "        or bypass with 'git commit --no-verify'."
fi
exit $status
# <<< sqldoc pre-commit hook <<<
""".format(marker=HOOK_MARKER)


def _git_dir(repo_root: str) -> str:
    """Resolve the real .git directory (handles worktrees where .git is a file
    pointing elsewhere). Falls back to <repo_root>/.git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if out:
            return out if os.path.isabs(out) else os.path.join(repo_root, out)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return os.path.join(repo_root, ".git")


def install_hooks(repo_root: str = ".", force: bool = False) -> dict:
    """Install the pre-commit hook into repo_root's git hooks directory.

    Returns a dict describing the result: {status, path, message}. status is one
    of 'installed', 'exists' (a non-sqldoc hook is already there and force is
    off), or 'not_a_repo'.
    """
    git_dir = _git_dir(repo_root)
    if not os.path.isdir(git_dir):
        return {"status": "not_a_repo", "path": git_dir,
                "message": f"Not a git repository (no {git_dir}). Run 'git init' first."}
    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "pre-commit")

    if os.path.exists(hook_path) and not force:
        try:
            with open(hook_path, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        except OSError:
            existing = ""
        if HOOK_MARKER not in existing:
            return {"status": "exists", "path": hook_path,
                    "message": (f"A pre-commit hook already exists at {hook_path} and was not "
                                "written by sqldoc. Re-run with --force to overwrite it, or "
                                "merge the sqldoc check in manually.")}

    with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(HOOK_SCRIPT)
    # Make the hook executable by its OWNER only (git runs hooks as the current
    # user). Least-privilege: no group/other execute bits (semgrep
    # insecure-file-permissions). No-op semantics on Windows, harmless.
    try:
        st = os.stat(hook_path)
        os.chmod(hook_path, st.st_mode | stat.S_IXUSR)
    except OSError:
        pass
    return {"status": "installed", "path": hook_path,
            "message": f"Installed sqldoc pre-commit hook at {hook_path}."}
