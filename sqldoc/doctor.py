"""Environment diagnostics for ``sqldoc doctor``.

A pre-flight self-check that answers "will sqldoc actually be able to run here?"
before a user hits a cryptic ODBC/driver error. Every check is a pure probe of
the local environment (installed packages, ODBC drivers, AI backends, config)
plus an optional live connection test; nothing here mutates state.

The check functions return :class:`Check` records so the CLI layer can render
them (and tests can assert on them) without any I/O of their own.
"""
from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field

from sqldoc import __version__

# Status levels, worst-last so ``max`` picks the overall severity.
OK = "ok"
WARN = "warn"
FAIL = "fail"
_SEVERITY = {OK: 0, WARN: 1, FAIL: 2}


@dataclass
class Check:
    name: str
    status: str          # OK / WARN / FAIL
    detail: str = ""
    hint: str = ""       # remediation suggestion shown for WARN/FAIL


@dataclass
class Report:
    checks: list = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.checks:
            return OK
        return max((c.status for c in self.checks), key=lambda s: _SEVERITY[s])

    @property
    def ok(self) -> bool:
        # A healthy environment has no FAILs (WARNs are advisory).
        return all(c.status != FAIL for c in self.checks)


# --- individual probes ------------------------------------------------------

def check_python() -> Check:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 9):
        return Check("Python", FAIL, f"{ver} on {platform.platform()}",
                     "sqldoc needs Python 3.9+.")
    return Check("Python", OK, f"{ver} on {platform.system()} {platform.release()}")


def check_sqldoc() -> Check:
    return Check("sqldoc", OK, f"v{__version__}")


def _list_odbc_drivers():
    """Return the list of installed ODBC drivers via pyodbc, or None if pyodbc
    itself is unavailable (so the caller can distinguish 'no pyodbc' from 'no
    drivers')."""
    try:
        import pyodbc
    except Exception:
        return None
    try:
        return list(pyodbc.drivers())
    except Exception:
        return []


def check_pyodbc() -> Check:
    try:
        import pyodbc  # noqa: F401
    except Exception as e:
        return Check("pyodbc", FAIL, f"not importable ({e})",
                     "pip install pyodbc (needed for SQL Server / Azure SQL).")
    return Check("pyodbc", OK, "installed")


def check_odbc_drivers() -> Check:
    """Report installed ODBC drivers and flag the common SQL Server ones.

    This is the check that catches the "ODBC Driver 17 vs 18" mismatch: it names
    exactly which SQL Server drivers are present so the user knows what to put in
    the `driver:` config key.
    """
    drivers = _list_odbc_drivers()
    if drivers is None:
        return Check("ODBC drivers", WARN, "pyodbc not available",
                     "Install pyodbc to enumerate ODBC drivers.")
    sql_drivers = [d for d in drivers if "SQL Server" in d]
    if not drivers:
        return Check("ODBC drivers", WARN, "none installed",
                     "Install the Microsoft ODBC Driver for SQL Server "
                     "(17 or 18) to connect to SQL Server / Azure SQL.")
    if not sql_drivers:
        return Check("ODBC drivers", WARN,
                     f"{len(drivers)} found, none for SQL Server",
                     "Install the Microsoft ODBC Driver for SQL Server (17 or 18).")
    has18 = any("18" in d for d in sql_drivers)
    detail = "; ".join(sql_drivers)
    if not has18:
        # The built-in default is Driver 18; if only 17 is present the user must
        # set `driver:` in .sqldoc.yml (or pass a matching connection string).
        return Check("ODBC drivers", WARN, detail,
                     "Default is 'ODBC Driver 18 for SQL Server' but it is not "
                     "installed. Set `driver: \"" + sql_drivers[0] + "\"` in "
                     ".sqldoc.yml, or install ODBC Driver 18.")
    return Check("ODBC drivers", OK, detail)


# Optional adapter drivers: (import module, human label, extra name).
_OPTIONAL_DRIVERS = [
    ("psycopg2", "PostgreSQL (psycopg2)", "postgres"),
    ("mysql.connector", "MySQL (mysql-connector-python)", "mysql"),
    ("snowflake.connector", "Snowflake", "snowflake"),
    ("oracledb", "Oracle", "oracle"),
    ("pymongo", "MongoDB", "mongodb"),
]


def check_optional_drivers() -> list:
    checks = []
    for module, label, extra in _OPTIONAL_DRIVERS:
        try:
            __import__(module)
            checks.append(Check(label, OK, "installed"))
        except Exception:
            checks.append(Check(label, OK, f"not installed (pip install sqldoc[{extra}])"))
    return checks


def check_ai_backends(ollama_probe=None) -> list:
    """Report AI backend readiness: cloud API keys present, Ollama reachable.

    ``ollama_probe`` is injectable for testing; by default it makes a short HTTP
    request to the local Ollama endpoint. AI is optional (``--no-ai`` works
    everywhere), so a missing backend is a WARN, never a FAIL.
    """
    checks = []
    for env, label in (("ANTHROPIC_API_KEY", "Anthropic"),
                       ("OPENAI_API_KEY", "OpenAI"),
                       ("GOOGLE_API_KEY", "Google Gemini")):
        if os.environ.get(env):
            checks.append(Check(f"AI: {label}", OK, "API key set"))
        else:
            checks.append(Check(f"AI: {label}", OK, f"no {env} (cloud backend unavailable)"))

    if ollama_probe is None:
        def ollama_probe():
            import requests
            base = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            requests.get(base.rstrip("/") + "/api/tags", timeout=2).raise_for_status()
            return True
    try:
        ollama_probe()
        checks.append(Check("AI: Ollama (local)", OK, "reachable"))
    except Exception:
        checks.append(Check("AI: Ollama (local)", OK,
                            "not reachable (start Ollama for local AI, or use "
                            "--no-ai / a cloud backend)"))
    return checks


def check_config(config_path: str = ".sqldoc.yml", exists=None, load=None) -> Check:
    """Validate an optional ``.sqldoc.yml``. ``exists``/``load`` are injectable
    for testing; by default they hit the filesystem / PyYAML."""
    if exists is None:
        exists = os.path.exists
    if not exists(config_path):
        return Check("Config", OK, f"no {config_path} (using CLI flags / defaults)")
    if load is None:
        def load(p):
            import yaml
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    try:
        data = load(config_path)
    except Exception as e:
        return Check("Config", FAIL, f"{config_path} is not valid: {e}",
                     "Fix the YAML syntax in your config file.")
    if not isinstance(data, dict):
        return Check("Config", FAIL, f"{config_path} is not a mapping",
                     "The top level of .sqldoc.yml must be key: value pairs.")
    notes = []
    if data.get("driver"):
        notes.append(f"driver={data['driver']!r}")
    if data.get("windows_auth") or data.get("windows-auth"):
        notes.append("windows_auth=on")
    detail = f"{config_path} loaded" + (f" ({', '.join(notes)})" if notes else "")
    return Check("Config", OK, detail)


def check_connection(conn_str: str, connect=None) -> Check:
    """Attempt a live connection and a trivial round-trip. ``connect`` is
    injectable for testing; by default it uses pyodbc."""
    if not conn_str:
        return Check("Connection", OK, "not tested (no connection provided)")
    if connect is None:
        def connect(cs):
            import pyodbc
            return pyodbc.connect(cs, timeout=5)
    try:
        conn = connect(conn_str)
    except Exception as e:
        return Check("Connection", FAIL, f"could not connect ({e})",
                     "Check server/credentials, the ODBC driver name, and "
                     "network access. `sqldoc doctor` above lists installed drivers.")
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
    except Exception as e:
        return Check("Connection", WARN, f"connected but test query failed ({e})")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return Check("Connection", OK, "connected + test query succeeded")


def run_checks(config_path: str = ".sqldoc.yml", conn_str: str = None,
               ollama_probe=None, connect=None) -> Report:
    """Run the full diagnostic suite and return a :class:`Report`."""
    report = Report()
    report.checks.append(check_sqldoc())
    report.checks.append(check_python())
    report.checks.append(check_pyodbc())
    report.checks.append(check_odbc_drivers())
    report.checks.extend(check_optional_drivers())
    report.checks.extend(check_ai_backends(ollama_probe=ollama_probe))
    report.checks.append(check_config(config_path))
    if conn_str:
        report.checks.append(check_connection(conn_str, connect=connect))
    return report
