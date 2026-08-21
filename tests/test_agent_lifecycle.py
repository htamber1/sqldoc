"""Daemon lifecycle: the pid file, the kill switch, and interrupted runs.

All three defects were found by actually starting the daemon and stopping it,
not by reading it -- they are exactly the class that only shows up when a real
process exists.

G1  `agent start --foreground` never wrote a pid file, so `agent stop` reported
    "sqldoc agent is not running" and left it running. There was no supported way
    to stop a foreground agent.

G2  `_terminate()` signalled only the pid in the pid file. Observed on this
    machine: `agent start` in a virtualenv produces TWO processes -- the venv
    launcher shim (whose pid is recorded) and the real daemon as its CHILD. On
    Windows, terminating a parent does not terminate its child, so the force-kill
    path could leave the actual daemon running with nothing left to find it by.

G3  A killed poll leaves its `runs` row at status='running' with finished_at
    NULL forever; nothing reconciles it, so `agent status` reports a long-dead
    poll as still in progress. Observed directly: a long poll interrupted by a
    forced stop is still 'running' in that store.

Offline: no database, no daemon, no network.
"""
import os

import pytest
from click.testing import CliRunner

from sqldoc.agent import pid_path, stop_flag_path, db_path
from sqldoc.agent.cli import agent as agent_group
from sqldoc.agent import cli as agent_cli
from sqldoc.agent.store import AgentStore


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own agent home; never the developer's real one."""
    home = tmp_path / "agenthome"
    home.mkdir()
    monkeypatch.setenv("SQLDOC_AGENT_HOME", str(home))
    return home


# ============================================================================
# G3 -- interrupted runs
# ============================================================================

def test_interrupted_run_is_reconciled():
    store = AgentStore(db_path())
    run_id = store.start_run("db1")
    assert store.last_run("db1")["status"] == "running"

    assert store.reconcile_interrupted_runs() == 1

    row = store.last_run("db1")
    assert row["status"] == "interrupted", (
        "a killed poll still reports as 'running'; it is indistinguishable "
        "from a live one forever")
    assert row["finished_at"], "interrupted run left with a NULL finished_at"
    assert row["error"]


def test_reconcile_leaves_completed_runs_alone():
    store = AgentStore(db_path())
    ok_id = store.start_run("db1")
    store.finish_run(ok_id, "ok")
    err_id = store.start_run("db2")
    store.finish_run(err_id, "error", "boom")

    assert store.reconcile_interrupted_runs() == 0
    assert store.last_run("db1")["status"] == "ok"
    assert store.last_run("db2")["status"] == "error"
    assert store.last_run("db2")["error"] == "boom"


def test_reconcile_preserves_an_existing_error_message():
    store = AgentStore(db_path())
    store.start_run("db1")
    with store._conn() as c:
        c.execute("UPDATE runs SET error='original' WHERE status='running'")
    store.reconcile_interrupted_runs()
    assert store.last_run("db1")["error"] == "original"


def test_reconcile_is_idempotent():
    store = AgentStore(db_path())
    store.start_run("db1")
    assert store.reconcile_interrupted_runs() == 1
    assert store.reconcile_interrupted_runs() == 0


def test_reconcile_handles_several_databases():
    store = AgentStore(db_path())
    for name in ("a", "b", "c"):
        store.start_run(name)
    assert store.reconcile_interrupted_runs() == 3
    for name in ("a", "b", "c"):
        assert store.last_run(name)["status"] == "interrupted"


def test_reconcile_on_an_empty_store_is_a_no_op():
    assert AgentStore(db_path()).reconcile_interrupted_runs() == 0


# ============================================================================
# G2 -- the kill switch must reach the whole process tree
# ============================================================================

def test_terminate_kills_the_tree_on_windows(monkeypatch):
    """The recorded pid may be a launcher shim whose CHILD is the real daemon.

    Tests the branch directly rather than monkeypatching os.name -- patching
    os.name globally breaks pathlib for the rest of the process.
    """
    calls = []
    monkeypatch.setattr(agent_cli.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    assert agent_cli._kill_tree_windows(4321) is True

    assert calls, "no kill was issued at all"
    cmd = calls[0]
    assert cmd[0] == "taskkill"
    assert "4321" in cmd
    assert "/T" in cmd, "kill is not tree-aware; a child daemon would be orphaned"
    assert "/F" in cmd


def test_terminate_kills_the_process_group_on_posix(monkeypatch):
    # raising=False: os.getpgid/os.killpg do not exist on Windows, where this
    # suite also has to run.
    killed = {}
    monkeypatch.setattr(os, "getpgid", lambda pid: 999, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.setdefault("pgid", pgid),
                        raising=False)
    assert agent_cli._kill_group_posix(4321) is True
    assert killed.get("pgid") == 999, "did not signal the process group"


def test_terminate_dispatches_to_the_right_branch(monkeypatch):
    """_terminate must use the tree/group killer, not a bare os.kill."""
    used = []
    monkeypatch.setattr(agent_cli, "_kill_tree_windows",
                        lambda pid: used.append("tree") or True)
    monkeypatch.setattr(agent_cli, "_kill_group_posix",
                        lambda pid: used.append("group") or True)
    monkeypatch.setattr(os, "kill",
                        lambda *a: pytest.fail("fell back to a single-pid kill"))
    agent_cli._terminate(4321)
    assert used and used[0] in ("tree", "group")


def test_terminate_falls_back_to_a_plain_signal(monkeypatch):
    """If the tree/group kill is unavailable, still signal something."""
    monkeypatch.setattr(agent_cli, "_kill_tree_windows", lambda pid: False)
    monkeypatch.setattr(agent_cli, "_kill_group_posix", lambda pid: False)
    sent = {}
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.setdefault("pid", pid))
    agent_cli._terminate(4321)
    assert sent.get("pid") == 4321


def test_terminate_survives_an_already_dead_pid(monkeypatch):
    monkeypatch.setattr(agent_cli.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("gone")))
    monkeypatch.setattr(agent_cli, "_kill_group_posix", lambda pid: False)
    monkeypatch.setattr(os, "kill", lambda *a: None)
    agent_cli._terminate(4321)          # must not raise


# ============================================================================
# G1 -- a foreground agent must be stoppable
# ============================================================================

def _fake_config(tmp_path):
    cfg = tmp_path / ".sqldoc.yml"
    cfg.write_text(
        "agent:\n"
        "  databases:\n"
        "    - name: d\n"
        "      connection_string: sqlite:///x\n",
        encoding="utf-8")
    return str(cfg)


def test_foreground_writes_a_pid_file_while_running(tmp_path, monkeypatch):
    """Without this, `agent stop` says 'not running' and the agent keeps going."""
    seen = {}

    def fake_run_foreground(config):
        seen["pid_file_exists"] = os.path.exists(pid_path())
        seen["recorded"] = int(open(pid_path(), encoding="utf-8").read().strip())

    monkeypatch.setattr(agent_cli, "_run_foreground", fake_run_foreground)
    result = CliRunner().invoke(agent_group,
                                ["start", "--foreground", "--config", _fake_config(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen.get("pid_file_exists"), (
        "no pid file while running in the foreground; `agent stop` cannot find it")
    assert seen["recorded"] == os.getpid()


def test_foreground_cleans_up_its_pid_file_on_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cli, "_run_foreground", lambda config: None)
    CliRunner().invoke(agent_group,
                       ["start", "--foreground", "--config", _fake_config(tmp_path)])
    assert not os.path.exists(pid_path()), "stale pid file left behind after exit"


def test_foreground_cleans_up_even_when_the_daemon_raises(tmp_path, monkeypatch):
    """Ctrl-C and crashes must not leave a pid file that blocks the next start."""
    def boom(config):
        raise KeyboardInterrupt()

    monkeypatch.setattr(agent_cli, "_run_foreground", boom)
    CliRunner().invoke(agent_group,
                       ["start", "--foreground", "--config", _fake_config(tmp_path)])
    assert not os.path.exists(pid_path())
    assert not os.path.exists(stop_flag_path())


def test_foreground_refuses_to_start_over_a_running_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cli, "_read_pid", lambda: 4321)
    monkeypatch.setattr(agent_cli, "pid_alive", lambda pid: True)
    monkeypatch.setattr(agent_cli, "_run_foreground",
                        lambda config: pytest.fail("started a second agent"))
    result = CliRunner().invoke(agent_group,
                                ["start", "--foreground", "--config", _fake_config(tmp_path)])
    assert result.exit_code != 0
    assert "already running" in result.output


def test_foreground_clears_a_stale_stop_flag_before_starting(tmp_path, monkeypatch):
    """A leftover stop flag would stop the new agent the instant it starts."""
    with open(stop_flag_path(), "w", encoding="utf-8") as f:
        f.write("stop")
    seen = {}
    monkeypatch.setattr(agent_cli, "_run_foreground",
                        lambda config: seen.setdefault("flag", os.path.exists(stop_flag_path())))
    CliRunner().invoke(agent_group,
                       ["start", "--foreground", "--config", _fake_config(tmp_path)])
    assert seen.get("flag") is False, "stale stop flag would stop the agent immediately"
