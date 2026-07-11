"""`sqldoc agent` commands: start / stop / status / logs (+ hidden _run).

`start` validates the config, then spawns a detached background process running
the daemon (`agent _run`), writing its PID to ~/.sqldoc/agent.pid and streaming
output to ~/.sqldoc/agent.log. `stop` asks the daemon to exit gracefully via a
stop-flag file (falling back to terminate). `status` reads the state DB; `logs`
tails the log file.
"""
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import click
import yaml

from sqldoc.agent import db_path, pid_path, log_path, stop_flag_path
from sqldoc.agent.config import parse_agent_config
from sqldoc.agent.daemon import run_daemon, watch_stop_flag
from sqldoc.agent.notify import Notifier
from sqldoc.agent.store import AgentStore


def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        raise click.UsageError(
            f"Config file not found: {path}. Create a .sqldoc.yml with an 'agent:' section.")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse(config: str):
    """Load + validate the agent config, surfacing errors as clean UsageErrors."""
    try:
        return parse_agent_config(_load_yaml(config))
    except ValueError as e:
        raise click.UsageError(str(e))


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_pid():
    try:
        with open(pid_path(), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_pid(pid: int):
    with open(pid_path(), "w", encoding="utf-8") as f:
        f.write(str(pid))


def _remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def pid_alive(pid) -> bool:
    """Cross-platform liveness check. On Windows, os.kill(pid, 0) would TERMINATE
    the process, so query the process handle instead."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


def _terminate(pid: int):
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, AttributeError):
        pass


@click.group()
def agent():
    """Run sqldoc as a persistent background database-monitoring daemon."""


@agent.command()
@click.option("--config", default=".sqldoc.yml", help="Path to .sqldoc.yml (with an 'agent:' section)")
@click.option("--foreground", is_flag=True, default=False,
              help="Run in the foreground (Ctrl-C to stop) instead of as a background daemon.")
def start(config, foreground):
    """Start the monitoring agent."""
    ac = _parse(config)   # validate before doing anything

    if foreground:
        click.echo(f"Running sqldoc agent in the foreground (Ctrl-C to stop). "
                   f"Dashboard: http://127.0.0.1:{ac.dashboard_port}")
        _run_foreground(config)
        return

    pid = _read_pid()
    if pid and pid_alive(pid):
        raise click.UsageError(
            f"sqldoc agent is already running (pid {pid}). Use 'sqldoc agent stop' first.")
    _remove(stop_flag_path())

    logf = open(log_path(), "a", encoding="utf-8")
    kwargs = {}
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-m", "sqldoc.cli", "agent", "_run", "--config", os.path.abspath(config)],
        stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creationflags, **kwargs)
    _write_pid(proc.pid)
    click.echo(f"sqldoc agent started (pid {proc.pid}) — monitoring "
               f"{len(ac.databases)} database(s) every {ac.interval_minutes}m.")
    click.echo(f"  Dashboard: http://127.0.0.1:{ac.dashboard_port}")
    click.echo(f"  Logs:      sqldoc agent logs -f")


@agent.command(name="_run", hidden=True)
@click.option("--config", default=".sqldoc.yml")
def _run(config):
    """(internal) Run the daemon in the foreground — spawned by `agent start`."""
    _run_foreground(config)


def _run_foreground(config):
    ac = _parse(config)
    store = AgentStore(db_path())
    notifier = Notifier(ac.notify)
    stop_event = threading.Event()
    _remove(stop_flag_path())

    def _handler(signum, frame):
        stop_event.set()
    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, AttributeError, OSError):
        pass
    threading.Thread(target=watch_stop_flag, args=(stop_flag_path(), stop_event),
                     daemon=True).start()

    run_daemon(ac, store, notifier, stop_event,
               log=lambda msg: print(f"{_ts()} {msg}", flush=True))


@agent.command()
def stop():
    """Gracefully stop the running agent."""
    pid = _read_pid()
    if not pid or not pid_alive(pid):
        click.echo("sqldoc agent is not running.")
        _remove(pid_path())
        return
    with open(stop_flag_path(), "w", encoding="utf-8") as f:
        f.write("stop")
    for _ in range(30):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    if pid_alive(pid):
        click.echo("Graceful stop timed out; terminating.")
        _terminate(pid)
        time.sleep(1)
    _remove(pid_path())
    _remove(stop_flag_path())
    click.echo("sqldoc agent stopped.")


@agent.command()
@click.option("--config", default=".sqldoc.yml", help="Path to .sqldoc.yml (for interval/dashboard info)")
def status(config):
    """Show what's being monitored and last run times."""
    pid = _read_pid()
    running = bool(pid and pid_alive(pid))
    click.echo(f"Agent: {'RUNNING (pid ' + str(pid) + ')' if running else 'stopped'}")
    try:
        ac = parse_agent_config(_load_yaml(config))
        click.echo(f"Interval: {ac.interval_minutes}m   "
                   f"Dashboard: http://127.0.0.1:{ac.dashboard_port}")
    except Exception:
        pass

    store = AgentStore(db_path())
    dbs = store.list_databases()
    if not dbs:
        click.echo("No monitored databases yet (the agent records data after its first poll).")
        return
    click.echo(f"Monitoring {len(dbs)} database(s):")
    for name in dbs:
        run = store.last_run(name) or {}
        m = store.latest_metric(name) or {}
        last = run.get("finished_at") or run.get("started_at") or "—"
        click.echo(f"  - {name}: last run {last} [{run.get('status', '—')}]  "
                   f"tables={m.get('tables', 0)} PII-score={m.get('pii_score', 0)} "
                   f"health-issues={m.get('health_issues', 0)}")


@agent.command()
@click.option("-n", "lines", default=40, help="Number of lines to show")
@click.option("-f", "follow", is_flag=True, default=False, help="Follow the log (like tail -f)")
def logs(lines, follow):
    """Show (and optionally follow) the agent log."""
    p = log_path()
    if not os.path.exists(p):
        click.echo("No agent log yet.")
        return
    with open(p, encoding="utf-8", errors="replace") as f:
        tail = f.readlines()[-lines:]
    for line in tail:
        click.echo(line.rstrip())
    if follow:
        with open(p, encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            try:
                while True:
                    line = f.readline()
                    if line:
                        click.echo(line.rstrip())
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                pass
