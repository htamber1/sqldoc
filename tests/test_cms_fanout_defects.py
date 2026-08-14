"""Regression tests for the two --cms fan-out defects found in field testing.

Both were observed against a real 9-server CMS group and neither had any test
coverage — `run_cms_bulk` was untested end to end before this file.

  1. A --cms run exited 0 even when part of the estate failed, so a scheduled
     fan-out could cover a fraction of the estate and still look clean to
     cron/CI. It now exits 2 when any server failed -- but only under the
     opt-in --fail-on-partial flag.

     The flag defaults to OFF deliberately. Making partial failure fatal by
     default would break every existing pipeline on upgrade, which is not
     something a release should do to people who did not ask for it. The
     default path is therefore byte-for-byte the old behaviour, and the tests
     below pin BOTH: exit 0 by default, exit 2 with the flag.

  2. Estate reports landed on the single-database default filename
     (documentation.html), silently overwriting a single-DB report. The
     intended `cms-<command>.html` fallback was dead code, because every
     CMS-capable command declares a non-None --output default, so
     `output or "cms-doc.html"` could never fire.

Pure unit tests against run_cms_bulk with the CMS layer stubbed — no database
and no CMS required.
"""
import pytest

import click

from sqldoc import cli
from sqldoc.cms_bulk import ServerResult

# The estate filenames the non-bulk CMS paths pass explicitly. run_cms_bulk
# derives its own as f"cms-{command}.html".
_CMS_DEFAULTS = {
    "executive": "cms-executive.html",
    "access review": "cms-access-review.html",
}


def _resolve_command(name):
    """Look up a command by its space-separated path, so 'access review'
    resolves through the access group."""
    node = cli.cli
    for part in name.split(" "):
        if not isinstance(node, click.Group):
            return None
        node = node.commands.get(part)
        if node is None:
            return None
    return node


@pytest.fixture
def fake_cms(monkeypatch, tmp_path):
    """Stub out inventory discovery, the fan-out itself, and the renderers.

    run_cms_bulk imports these lazily inside the function body, so patching the
    owning modules is what reaches it.
    """
    from sqldoc import cms as cms_mod, cms_bulk, cms_renderer

    state = {"rendered_to": None, "results": []}

    monkeypatch.setattr(cms_mod, "has_inventory", lambda cfg: True)
    monkeypatch.setattr(cms_mod, "from_config", lambda cfg: object())
    monkeypatch.setattr(cms_mod, "select_servers",
                        lambda inv, group=None, recursive=True: ["s1", "s2"])
    monkeypatch.setattr(cms_bulk, "run_bulk",
                        lambda *a, **k: state["results"])

    def _render(command, results, out, group=None):
        state["rendered_to"] = out

    monkeypatch.setattr(cms_renderer, "render_bulk_html", _render)
    monkeypatch.setattr(cms_renderer, "build_bulk_json", lambda c, r: {})
    monkeypatch.chdir(tmp_path)
    return state


def _run(command_name="doc", output=None, fail_on_partial=False):
    return cli.run_cms_bulk(command_name, {}, True, None, None, None,
                            output, None, 8, fail_on_partial=fail_on_partial)


def _ok(name):
    return ServerResult(server=name, host=name, ok=True)


def _failed(name, error="login timeout expired"):
    return ServerResult(server=name, host=name, ok=False, error=error)


class TestExitCodeDefault:
    """The default path must be exactly what it was before --fail-on-partial
    existed, so upgrading cannot break a pipeline that checks the exit code."""

    def test_all_servers_ok_returns_normally(self, fake_cms):
        fake_cms["results"] = [_ok("s1"), _ok("s2")]
        assert _run() is True

    def test_partial_failure_does_not_exit_by_default(self, fake_cms):
        """The upgrade-safety guarantee. A pipeline on the old version saw exit
        0 here; it must still see exit 0 after upgrading."""
        fake_cms["results"] = [_ok("s1"), _failed("s2")]
        assert _run() is True

    def test_total_failure_does_not_exit_by_default(self, fake_cms):
        fake_cms["results"] = [_failed("s1"), _failed("s2")]
        assert _run() is True

    def test_failures_are_still_reported_when_not_fatal(self, fake_cms, capsys):
        """Not exiting is not the same as staying quiet: the operator must still
        be told which servers failed, flag or no flag."""
        fake_cms["results"] = [_ok("s1"), _failed("s2", "login timeout expired")]
        _run()
        err = capsys.readouterr().err
        assert "s2" in err and "login timeout expired" in err


class TestExitCodeFailOnPartial:
    """Defect 1, opt-in: with --fail-on-partial, a partly-covered estate is a
    failure and has to reach the exit code."""

    def test_any_server_failed_exits_2(self, fake_cms):
        fake_cms["results"] = [_ok("s1"), _failed("s2")]
        with pytest.raises(SystemExit) as exc:
            _run(fail_on_partial=True)
        assert exc.value.code == 2, (
            "A --cms run that covered only part of the estate exited 0, so a "
            "scheduled fan-out looks successful while silently skipping servers.")

    def test_every_server_failed_exits_2(self, fake_cms):
        fake_cms["results"] = [_failed("s1"), _failed("s2")]
        with pytest.raises(SystemExit) as exc:
            _run(fail_on_partial=True)
        assert exc.value.code == 2

    def test_all_ok_returns_normally_under_the_flag(self, fake_cms):
        """The flag makes failure fatal; it must not invent a failure."""
        fake_cms["results"] = [_ok("s1"), _ok("s2")]
        assert _run(fail_on_partial=True) is True

    def test_report_is_still_written_when_servers_failed(self, fake_cms):
        """The exit code must not cost the operator the partial report — the
        successful servers' results are exactly what says what was covered."""
        fake_cms["results"] = [_ok("s1"), _failed("s2")]
        with pytest.raises(SystemExit):
            _run(fail_on_partial=True)
        assert fake_cms["rendered_to"] == "cms-doc.html"

    def test_empty_estate_does_not_exit_2(self, fake_cms):
        """No servers is not the same as servers that failed."""
        fake_cms["results"] = []
        assert _run(fail_on_partial=True) is True


class TestFailOnPartialIsDeclaredEverywhere:
    """The flag must exist on every --cms-capable command, and mean the same
    thing on each. A flag that silently does nothing on two of the three estate
    paths is worse than no flag."""

    def test_every_cms_command_declares_the_flag(self):
        names = sorted(cli._SINGLE_DB_OUTPUT_DEFAULTS)
        missing = []
        for name in names:
            cmd = _resolve_command(name)
            if cmd is None:
                continue
            if not any(p.name == "fail_on_partial" for p in cmd.params):
                missing.append(name)
        assert not missing, f"--fail-on-partial missing from: {missing}"

    def test_flag_defaults_to_off_on_every_command(self):
        for name in sorted(cli._SINGLE_DB_OUTPUT_DEFAULTS):
            cmd = _resolve_command(name)
            if cmd is None:
                continue
            param = next((p for p in cmd.params if p.name == "fail_on_partial"), None)
            if param is None:
                continue
            assert param.default is False, (
                f"{name} defaults --fail-on-partial to {param.default!r}; the "
                "whole point is that the default behaviour is unchanged.")

    def test_helper_only_exits_when_both_conditions_hold(self):
        """The gate itself, independent of any command wiring."""
        assert cli._exit_on_partial(False, 3) is None
        assert cli._exit_on_partial(True, 0) is None
        assert cli._exit_on_partial(False, 0) is None
        with pytest.raises(SystemExit) as exc:
            cli._exit_on_partial(True, 1)
        assert exc.value.code == 2


class TestOutputNaming:
    """Defect 2: the estate report must not land on the single-DB default."""

    def test_unset_output_uses_estate_name(self, fake_cms):
        fake_cms["results"] = [_ok("s1")]
        _run(output=None)
        assert fake_cms["rendered_to"] == "cms-doc.html"

    @pytest.mark.parametrize("command,default", [
        ("doc", "documentation.html"),
        ("scan", "pii-report.html"),
        ("health", "health-report.html"),
        ("secure", "security-report.html"),
    ])
    def test_click_filled_default_is_redirected(self, fake_cms, command, default):
        """click always supplies its per-command default, so an untouched
        --output must be treated as 'not explicitly set' or the estate report
        overwrites the single-database one."""
        fake_cms["results"] = [_ok("s1")]
        _run(command_name=command, output=default)
        assert fake_cms["rendered_to"] == f"cms-{command}.html"

    def test_explicit_custom_output_is_honoured(self, fake_cms):
        fake_cms["results"] = [_ok("s1")]
        _run(output="estate/q3-audit.html")
        assert fake_cms["rendered_to"] == "estate/q3-audit.html"

    def test_every_cms_capable_command_is_in_the_defaults_map(self):
        """A command that gained --cms without an entry here would write its
        estate report straight over its single-database report."""
        missing = []
        for name, cmd in cli.cli.commands.items():
            takes_cms = any(p.name == "use_cms" for p in cmd.params)
            has_output = any(p.name == "output" for p in cmd.params)
            if takes_cms and has_output and name not in cli._SINGLE_DB_OUTPUT_DEFAULTS:
                missing.append(name)
        assert not missing, (
            "CMS-capable command(s) absent from _SINGLE_DB_OUTPUT_DEFAULTS: "
            + ", ".join(sorted(missing)))

    def test_defaults_map_matches_the_commands_click_declares(self):
        """The map is only correct while it mirrors each command's real --output
        default; a drifted entry silently restores the overwrite bug."""
        for command, default in cli._SINGLE_DB_OUTPUT_DEFAULTS.items():
            cmd = _resolve_command(command)
            assert cmd is not None, f"{command} is no longer a CLI command"
            opt = next((p for p in cmd.params if p.name == "output"), None)
            assert opt is not None, f"{command} has no --output option"
            assert opt.default == default, (
                f"{command}'s --output default is {opt.default!r} but "
                f"_SINGLE_DB_OUTPUT_DEFAULTS says {default!r}; a --cms run "
                f"would write the estate report over the single-DB report.")


class TestSiblingCmsPaths:
    """executive --cms and access review --cms do not go through run_cms_bulk
    (they use cms_executive.py / access/cms_review.py), and both carried the
    same dead-fallback bug: `output or "cms-...html"` can never fire because
    click always supplies the command's own default.

    access review was the worse of the two — its estate fallback string was
    identical to its single-DB default, so there was no distinct name at all.
    """

    @pytest.mark.parametrize("command,single_db,expected", [
        ("executive", "executive-summary.html", "cms-executive.html"),
        ("access review", "access-review.html", "cms-access-review.html"),
    ])
    def test_click_filled_default_is_redirected(self, command, single_db, expected):
        assert cli._cms_output(command, single_db, expected) == expected

    @pytest.mark.parametrize("command,expected", [
        ("executive", "cms-executive.html"),
        ("access review", "cms-access-review.html"),
    ])
    def test_unset_output_uses_estate_name(self, command, expected):
        assert cli._cms_output(command, None, expected) == expected

    def test_explicit_custom_output_is_honoured(self):
        assert cli._cms_output("executive", "board.html",
                               "cms-executive.html") == "board.html"

    def test_estate_name_never_equals_the_single_db_default(self):
        """The invariant behind both bugs."""
        for command, single_db in cli._SINGLE_DB_OUTPUT_DEFAULTS.items():
            resolved = cli._cms_output(command, single_db,
                                       _CMS_DEFAULTS.get(command))
            assert resolved != single_db, (
                f"{command} --cms still resolves to {single_db!r}, the same "
                f"path as its single-database report.")
