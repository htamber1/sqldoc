"""Round 4 - unreachable AI backend: probe once, degrade cleanly, exit honestly.

The bug: every AI-capable command discovered an unreachable backend once PER
OBJECT. `doc` queues one task per table AND per column AND per procedure, each
retried 3x with exponential backoff, and each per-object failure was swallowed
by the worker. On a 308-table / 7,501-column / 1,261-proc database that is
~9,070 objects x 4 attempts = ~36,280 doomed connections. Measured against a
closed port it projects to ~7.6 hours -- and the command still exited 0, having
produced exactly the document `--no-ai` produces in seconds.

The fix has two independent halves, and these tests cover both:

  * `probe_backend()` -- one cheap reachability check per backend per process,
    called by each command before any fan-out;
  * the `_DOWN` latch + `_is_unreachable()` -- the backstop, so that even a code
    path that never probes stops after the FIRST hard failure instead of
    repeating it thousands of times.

Everything here runs offline. The "unreachable backend" is a port that is
genuinely closed (bound, then released), so no test depends on Ollama being
absent -- they would still be meaningful on a machine where it is running.
"""
import os
import socket
import time

import pytest
import requests

from sqldoc import ai


# --------------------------------------------------------------- fixtures ---

@pytest.fixture(autouse=True)
def _clean_backend_state():
    """Each test starts with no probe cache and no latched backend.

    Tolerant of the pre-fix module so this file still COLLECTS against an
    unpatched tree -- the point is for the tests to fail on the behaviour they
    assert, not to blow up in setup.
    """
    reset = getattr(ai, "reset_backend_state", lambda: None)
    reset()
    yield
    reset()


@pytest.fixture
def closed_port():
    """A port nothing is listening on: bind it, read the number, release it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def dead_ollama(monkeypatch, closed_port):
    """Point OLLAMA_HOST at the closed port for the duration of a test."""
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{closed_port}")
    getattr(ai, "reset_backend_state", lambda: None)()
    return f"http://127.0.0.1:{closed_port}"


def _table(name="T", ncols=0):
    from sqldoc.extractor import Table, Column
    cols = [Column(name=f"c{i}", data_type="int", max_length=None, is_nullable=False,
                   is_primary_key=False, is_foreign_key=False,
                   references_table=None, references_column=None)
            for i in range(ncols)]
    return Table(schema="dbo", name=name, row_count=0, columns=cols,
                 indexes=[], triggers=[], check_constraints=[], unique_constraints=[])


# ------------------------------------------------------- endpoint resolution -

def test_ollama_endpoint_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert ai.ollama_base_url() == "http://localhost:11434"


def test_ollama_endpoint_honours_ollama_host(monkeypatch):
    """The notice must name the endpoint actually in use, not a hard-coded one."""
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box:11434")
    assert ai.ollama_base_url() == "http://gpu-box:11434"


def test_ollama_endpoint_accepts_bare_host_port(monkeypatch):
    """OLLAMA_HOST is conventionally a bare host:port; accept it like ollama does."""
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box:11434")
    assert ai.ollama_base_url() == "http://gpu-box:11434"


def test_ollama_endpoint_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box:11434/")
    assert ai.ollama_base_url() == "http://gpu-box:11434"


def test_call_ollama_uses_the_configured_endpoint(monkeypatch):
    """Regression: the URL was hard-coded, so OLLAMA_HOST was silently ignored."""
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box:11434")
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        raise RuntimeError("stop here")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(Exception):
        ai._call_ollama("hi", "m")
    assert seen["url"] == "http://gpu-box:11434/api/generate"


def test_backend_endpoint_is_named_for_every_backend():
    """Every backend can describe itself, so the notice is never blank."""
    for backend in ai.ALL_BACKENDS:
        assert ai.backend_endpoint(backend)


# ------------------------------------------------------------ probe_backend -

def test_probe_reports_unreachable_ollama(dead_ollama):
    ok, endpoint, reason = ai.probe_backend(mode="local")
    assert ok is False
    assert endpoint == dead_ollama
    assert reason and "Error" in reason or reason


def test_probe_reports_reachable_ollama(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _ok_response())
    ok, endpoint, reason = ai.probe_backend(mode="local")
    assert ok is True
    assert reason == "reachable"


def _ok_response():
    r = requests.Response()
    r.status_code = 200
    return r


def test_probe_is_memoized_per_process(dead_ollama, monkeypatch):
    """Cost is one check, not one per command or per object."""
    calls = {"n": 0}
    real_get = requests.get

    def counting_get(*a, **k):
        calls["n"] += 1
        return real_get(*a, **k)

    monkeypatch.setattr(requests, "get", counting_get)
    for _ in range(5):
        ai.probe_backend(mode="local")
    assert calls["n"] == 1


def test_probe_latches_the_backend_down(dead_ollama):
    assert ai.backend_down("ollama") is None
    ai.probe_backend(mode="local")
    down = ai.backend_down("ollama")
    assert down is not None
    endpoint, reason = down
    assert endpoint == dead_ollama


def test_probe_never_makes_a_billable_cloud_call(monkeypatch):
    """A cloud probe checks credentials only -- it must not hit the network."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def explode(*a, **k):
        raise AssertionError("probe made a network call")

    monkeypatch.setattr(requests, "get", explode)
    monkeypatch.setattr(requests, "post", explode)
    ok, endpoint, reason = ai.probe_backend(mode="cloud", backend="anthropic")
    assert ok is True


def test_probe_flags_missing_cloud_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, endpoint, reason = ai.probe_backend(mode="cloud", backend="anthropic")
    assert ok is False
    assert "ANTHROPIC_API_KEY" in reason


@pytest.mark.parametrize("backend,env", [
    ("openai", "OPENAI_API_KEY"),
    ("gemini", "GOOGLE_API_KEY"),
])
def test_probe_is_backend_agnostic(monkeypatch, backend, env):
    """Same question, same shape of answer, for every backend."""
    monkeypatch.delenv(env, raising=False)
    if backend == "gemini":
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ok, endpoint, reason = ai.probe_backend(mode="cloud", backend=backend)
    assert ok is False
    assert isinstance(endpoint, str) and endpoint
    assert env.split("_")[0].lower() in reason.lower() or env in reason


def test_probe_rejects_an_unknown_backend():
    ok, endpoint, reason = ai.probe_backend(mode="local", backend="notabackend")
    assert ok is False
    assert "notabackend" in reason


# ------------------------------------------------------- failure classifier -

@pytest.mark.parametrize("make_exc", [
    lambda: requests.exceptions.ConnectionError("refused"),
    lambda: requests.exceptions.ConnectTimeout("no route"),
    lambda: ImportError("no sdk"),
    lambda: ai.BackendUnavailable("ollama", "http://x", "down"),
], ids=["connection-refused", "connect-timeout", "missing-sdk", "backend-unavailable"])
def test_unreachable_errors_are_classified_as_permanent(make_exc):
    # Built lazily: constructing BackendUnavailable at import time made this
    # file uncollectable against a pre-fix tree.
    assert ai._is_unreachable(make_exc()) is True


@pytest.mark.parametrize("exc", [
    requests.exceptions.ReadTimeout("slow"),
    ValueError("garbage response"),
])
def test_transient_errors_are_not_classified_as_permanent(exc):
    """A backend that is THERE but slow or flaky must still be retried."""
    assert ai._is_unreachable(exc) is False


@pytest.mark.parametrize("code,permanent", [
    (401, True), (403, True), (404, True),
    (429, False), (500, False), (503, False),
])
def test_http_status_classification(code, permanent):
    """Auth/not-found are permanent; rate-limit and 5xx are worth a retry."""
    resp = requests.Response()
    resp.status_code = code
    err = requests.exceptions.HTTPError("boom")
    err.response = resp
    assert ai._is_unreachable(err) is permanent


# -------------------------------------------------------------- _retry ------

def test_retry_does_not_retry_an_unreachable_backend():
    """The whole bug in one assertion: 4 attempts becomes 1."""
    calls = {"n": 0}

    def always_refused():
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("refused")

    with pytest.raises(Exception) as exc_info:
        ai._retry(always_refused, "ollama:m", backend="ollama")
    assert type(exc_info.value).__name__ == "BackendUnavailable", (
        "an unreachable backend must raise BackendUnavailable, not be retried")
    assert calls["n"] == 1, (
        f"unreachable backend was attempted {calls['n']} times; it must be attempted once")


def test_retry_still_retries_a_transient_failure(monkeypatch):
    """The retry behaviour that was worth having is preserved."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ReadTimeout("slow")
        return "ok"

    assert ai._retry(flaky, "ollama:m", backend="ollama") == "ok"
    assert calls["n"] == 3


def test_retry_gives_up_after_max_attempts_on_transient(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_slow():
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout("slow")

    with pytest.raises(requests.exceptions.ReadTimeout):
        ai._retry(always_slow, "ollama:m", backend="ollama")
    assert calls["n"] == ai.MAX_ATTEMPTS


def test_retry_fails_fast_once_the_backend_is_latched():
    """Second and subsequent callers do not touch the network at all."""
    ai.mark_backend_down("ollama", "http://x", "refused")
    calls = {"n": 0}

    def should_not_run():
        calls["n"] += 1
        return "nope"

    with pytest.raises(ai.BackendUnavailable):
        ai._retry(should_not_run, "ollama:m", backend="ollama")
    assert calls["n"] == 0


def test_retry_infers_the_backend_from_the_label():
    """Callers that pass only "backend:model" still get latch behaviour."""
    ai.mark_backend_down("anthropic", "api", "no key")
    with pytest.raises(ai.BackendUnavailable):
        ai._retry(lambda: "x", "anthropic:claude-haiku-4-5")


def test_latch_keeps_the_first_reason():
    ai.mark_backend_down("ollama", "http://a", "first")
    ai.mark_backend_down("ollama", "http://b", "second")
    assert ai.backend_down("ollama") == ("http://a", "first")


def test_reset_clears_probe_and_latch(dead_ollama):
    ai.probe_backend(mode="local")
    assert ai.degraded() is True
    ai.reset_backend_state()
    assert ai.degraded() is False
    assert ai.backend_down("ollama") is None


def test_degraded_detail_names_the_backend():
    ai.mark_backend_down("ollama", "http://x", "refused")
    backend, endpoint, reason = ai.degraded_detail()
    assert (backend, endpoint, reason) == ("ollama", "http://x", "refused")


# ------------------------------------------------- the fan-out, end to end --

def test_enrich_stops_after_the_first_hard_failure(dead_ollama):
    """The regression that cost hours: attempts must NOT scale with object count.

    Unpatched this made 4 attempts per object (200 tables x 4 = 800 doomed
    connections plus ~7.6s of backoff each). Patched, the latch bounds attempts
    by the pool width, no matter how large the database is.
    """
    attempts = {"n": 0}
    real_post = requests.post

    def counting_post(*a, **kw):
        attempts["n"] += 1
        return real_post(*a, **kw)

    import sqldoc.ai as ai_mod
    orig = ai_mod.requests.post
    ai_mod.requests.post = counting_post
    try:
        tables = [_table(f"T{i}") for i in range(200)]
        ai.enrich_tables(tables, mode="local", concurrency=4, cache=None)
    finally:
        ai_mod.requests.post = orig

    assert attempts["n"] <= 4 * 4, f"expected the latch to bound attempts, got {attempts['n']}"
    assert attempts["n"] < 200, "attempts still scale with object count"


def test_enrich_does_not_raise_when_the_backend_is_down(dead_ollama):
    """Degrading means the caller still gets its objects back, undescribed."""
    tables = [_table("A"), _table("B")]
    out = ai.enrich_tables(tables, mode="local", concurrency=2, cache=None)
    assert out is tables
    assert all(not t.description for t in out)


def test_enrich_reports_honest_stats(dead_ollama):
    """`[6/6] descriptions generated` used to print after six failures."""
    stats = {}
    ai.enrich_tables([_table("A"), _table("B")], mode="local", concurrency=2,
                     cache=None, stats_out=stats)
    assert stats["total"] == 2
    assert stats["ok"] == 0
    assert stats["unavailable"] == 2


def test_enrich_stats_accumulate_across_calls(dead_ollama):
    """doc passes one dict through tables, views and procedures."""
    stats = {}
    ai.enrich_tables([_table("A")], mode="local", concurrency=1, cache=None, stats_out=stats)
    ai.enrich_tables([_table("B")], mode="local", concurrency=1, cache=None, stats_out=stats)
    assert stats["total"] == 2


def test_cached_descriptions_still_work_with_a_dead_backend(dead_ollama):
    """A cache hit needs no backend, so an outage must not discard cached work."""
    table = _table("A")
    key = ai._key("llama3.1:8b", "table", ai._sig_table(table, False))
    cache = {"version": ai.CACHE_VERSION, "entries": {key: "from cache"}}
    stats = {}
    ai.enrich_tables([table], mode="local", model="llama3.1:8b",
                     concurrency=1, cache=cache, stats_out=stats)
    assert table.description == "from cache"
    assert stats["total"] == 0


def test_enrich_is_fast_when_the_backend_is_down(dead_ollama):
    """Wall-clock guard: 100 objects must not cost 100 backoff cycles.

    Unpatched, 100 objects at concurrency 8 took ~5 minutes against a closed
    port. The bound here is deliberately loose so it is not flaky on a slow
    machine, while still failing outright if per-object retrying comes back.
    """
    tables = [_table(f"T{i}") for i in range(100)]
    t0 = time.time()
    ai.enrich_tables(tables, mode="local", concurrency=8, cache=None)
    assert time.time() - t0 < 30


# ============================================================================
# --fail-on-partial: one flag, one meaning
# ============================================================================
# The degraded exit is gated by the SAME flag and the SAME helper the --cms
# estate paths use. Default stays 0 so upgrading cannot turn a green CI job red;
# the notice is printed either way, because that half is what fixes the silent
# wrongness and is not optional.

import click
from click.testing import CliRunner

from sqldoc import cli as cli_mod


AI_COMMANDS = ["doc", "insights", "waits", "plans", "deadlocks", "scan"]


def _params(command_name):
    cmd = cli_mod.cli.commands[command_name]
    return {p.name for p in cmd.params}


@pytest.mark.parametrize("command_name", AI_COMMANDS)
def test_every_ai_command_exposes_fail_on_partial(command_name):
    """The flag must exist on all of them, or it means two different things."""
    assert "fail_on_partial" in _params(command_name), (
        f"{command_name} can degrade its AI output but has no --fail-on-partial")


@pytest.mark.parametrize("command_name", AI_COMMANDS)
def test_fail_on_partial_defaults_to_off(command_name):
    """Default OFF is the whole point: an upgrade must not break a pipeline."""
    cmd = cli_mod.cli.commands[command_name]
    param = next(p for p in cmd.params if p.name == "fail_on_partial")
    assert param.default is False
    assert param.is_flag


def test_fail_on_partial_help_covers_both_meanings():
    """One flag, one documented meaning, covering estate AND AI partiality."""
    cmd = cli_mod.cli.commands["doc"]
    param = next(p for p in cmd.params if p.name == "fail_on_partial")
    help_text = (param.help or "").lower()
    assert "cms" in help_text, "help no longer mentions the estate case"
    # Not a bare "ai" substring test -- "ai" is inside "fail", so that would
    # have passed against the pre-fix help text by accident.
    assert "backend was unreachable" in help_text, (
        "help does not mention the AI case")


def test_degraded_ai_reuses_exit_on_partial(monkeypatch):
    """Not a parallel mechanism: the exit must go through _exit_on_partial."""
    seen = {}

    def spy(fail_on_partial, failed_count):
        seen["args"] = (fail_on_partial, failed_count)

    monkeypatch.setattr(cli_mod, "_exit_on_partial", spy)
    cli_mod._finish_degraded_ai("backend down", True, "AI descriptions")
    assert seen["args"] == (True, 1)


def test_finish_degraded_ai_default_does_not_exit():
    """Default path: the document was written, so this is a clean exit 0."""
    cli_mod._finish_degraded_ai("backend down", False, "AI descriptions")
    # no SystemExit == pass


def test_finish_degraded_ai_with_flag_exits_2():
    with pytest.raises(SystemExit) as exc_info:
        cli_mod._finish_degraded_ai("backend down", True, "AI descriptions")
    assert exc_info.value.code == 2


def test_finish_degraded_ai_is_silent_when_not_degraded():
    """A healthy AI run must neither print nor exit."""
    cli_mod._finish_degraded_ai(None, True, "AI descriptions")


def test_degradation_is_announced_regardless_of_the_flag(capsys):
    """The notice is NOT gated -- gating it would restore the silent wrongness."""
    for flag in (False, True):
        try:
            cli_mod._finish_degraded_ai("backend down", flag, "AI descriptions")
        except SystemExit:
            pass
        err = capsys.readouterr().err
        assert "AI backend was unreachable" in err, f"no notice with flag={flag}"


def test_default_notice_tells_the_user_how_to_make_it_fail(capsys):
    cli_mod._finish_degraded_ai("backend down", False, "AI descriptions")
    assert "--fail-on-partial" in capsys.readouterr().err


def test_exit_on_partial_still_serves_the_estate_case():
    """The shared helper's original behaviour must be untouched."""
    cli_mod._exit_on_partial(False, 3)          # flag off -> no exit
    cli_mod._exit_on_partial(True, 0)           # nothing failed -> no exit
    with pytest.raises(SystemExit) as exc_info:
        cli_mod._exit_on_partial(True, 3)
    assert exc_info.value.code == 2


def test_no_ai_is_not_partial():
    """--no-ai asked for the deterministic output and got it: never exit 2.

    `_ai_preflight` returns degraded=None for --no-ai, so the tail is a no-op
    even under the flag.
    """
    no_ai, degraded = cli_mod._ai_preflight("local", None, None, True,
                                            degrade_to="schema-only documentation")
    assert no_ai is True
    assert degraded is None
    cli_mod._finish_degraded_ai(degraded, True, "AI descriptions")   # must not exit


def test_preflight_reports_degraded_when_backend_is_down(dead_ollama, capsys):
    no_ai, degraded = cli_mod._ai_preflight("local", None, None, False,
                                            degrade_to="schema-only documentation")
    assert no_ai is True
    assert degraded
    err = capsys.readouterr().err
    assert "unreachable" in err
    assert dead_ollama in err, "the notice must name the endpoint actually in use"
