"""Shared helpers for the ``tests/live/`` validation scripts.

Unlike ``tests/integration/`` (which targets the local Docker sample databases),
these scripts validate the **mock-only** features — the cloud dialects, the
OpenAI/Gemini AI backends, the publishing/ticketing integrations, the
notification channels, and the identity providers — against **real** services a
developer has credentials for. Every check is **skip-gated**: it runs only when
the relevant env var is set or the relevant section exists in the live config,
and skips cleanly otherwise, so `pytest tests/live` is safe to run anywhere.

Two ways to supply credentials:

* **Connection strings / API keys** come from **environment variables**
  (e.g. ``SQLDOC_TEST_SNOWFLAKE``, ``OPENAI_API_KEY``).
* **Service config** (integrations, notifications, identity providers) comes
  from a **live config file** — a normal ``.sqldoc.yml`` with the relevant
  sections filled in. Point to it with ``SQLDOC_LIVE_CONFIG`` (default
  ``tests/live/sqldoc.live.yml``). A template is in
  ``tests/live/sqldoc.live.example.yml``.

Run everything:  ``pytest tests/live -v``
Run one area:    ``pytest tests/live/test_dialects_live.py -v``
"""
import os

import pytest
from click.testing import CliRunner


# --- live config -----------------------------------------------------------

def live_config_path() -> str:
    return os.environ.get("SQLDOC_LIVE_CONFIG", "tests/live/sqldoc.live.yml")


_CONFIG_CACHE = {}


def live_config() -> dict:
    path = live_config_path()
    if path not in _CONFIG_CACHE:
        data = {}
        if os.path.exists(path):
            import yaml
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        _CONFIG_CACHE[path] = data if isinstance(data, dict) else {}
    return _CONFIG_CACHE[path]


def has_section(key: str) -> bool:
    """True if the live config has a non-empty top-level ``key:`` section."""
    return bool(live_config().get(key))


# --- skip gates ------------------------------------------------------------

def requires_env(*names: str):
    """Skip unless every named environment variable is set + non-empty."""
    missing = [n for n in names if not os.environ.get(n)]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"set env {', '.join(missing)} to run this live check")


def requires_section(key: str):
    """Skip unless the live config has a ``key:`` section."""
    return pytest.mark.skipif(
        not has_section(key),
        reason=(f"add a '{key}:' section to {live_config_path()} "
                f"(see tests/live/sqldoc.live.example.yml) to run this live check"))


# --- CLI runner ------------------------------------------------------------

def run(args, with_config: bool = True):
    """Invoke the sqldoc CLI with args (returns the click Result). When
    ``with_config`` and the live config file exists, ``--config <path>`` is
    appended unless already present."""
    from sqldoc import cli
    argv = list(args)
    path = live_config_path()
    if with_config and os.path.exists(path) and "--config" not in argv:
        argv += ["--config", path]
    return CliRunner().invoke(cli.cli, argv, catch_exceptions=False)


def env(name: str, default=None):
    return os.environ.get(name, default)
