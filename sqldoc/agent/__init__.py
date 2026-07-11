"""sqldoc agent — a persistent background monitoring daemon.

Turns sqldoc into a living database monitoring system: it polls each configured
database on an interval, diffs the schema against the last snapshot, re-generates
AI documentation only for changed objects (reusing the description cache), tracks
health + PII trends over time, serves an always-current local dashboard, and
sends Slack/email alerts on schema changes, new PII findings, and health
degradation.

All daemon state lives in a local SQLite database (default ``~/.sqldoc/agent.db``)
managed by :class:`sqldoc.agent.store.AgentStore`.
"""
import os

# Default directory for agent state (db, pid, log, stop-flag). Overridable via
# SQLDOC_AGENT_HOME so tests and side-by-side agents can isolate their state.
AGENT_HOME = os.environ.get("SQLDOC_AGENT_HOME") or os.path.join(
    os.path.expanduser("~"), ".sqldoc")


def agent_home() -> str:
    """Resolve the agent home directory at call time (honours SQLDOC_AGENT_HOME)."""
    return os.environ.get("SQLDOC_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".sqldoc")


def path_in_home(name: str) -> str:
    home = agent_home()
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, name)


def db_path() -> str:
    return path_in_home("agent.db")


def pid_path() -> str:
    return path_in_home("agent.pid")


def log_path() -> str:
    return path_in_home("agent.log")


def stop_flag_path() -> str:
    return path_in_home("agent.stop")
