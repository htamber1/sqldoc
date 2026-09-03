"""Regression tests for the AI backend probe stampede (v3.2.0, patch 12).

Found running the agent against two databases concurrently -- the first time two
real poller threads have ever run -- while the AI backend was unreachable.

`probe_backend()` promises in its own docstring that it is "memoized per backend
per process, so calling it from several commands -- or twice in one command --
costs one check". It reads the `_PROBED` memo under `_DOWN_LOCK`, RELEASES the
lock, performs the probe, and only then writes the memo. Threads that arrive
inside that window all miss the memo and all probe: N concurrent callers cost N
checks, not one.

That is not a rare race. `run_daemon` starts every poller thread at once and
each polls immediately, so simultaneous arrival is the normal shape of cycle 1.

The fix serialises the probe per backend behind `_probe_lock(backend)` and
re-reads the memo after acquiring it (double-checked locking), so whoever waits
gets the winner's result. `_DOWN_LOCK` is deliberately NOT used for this: it
guards the state dicts and must never be held across a network call, or a worker
calling `backend_down()` mid-fan-out would block for PROBE_TIMEOUT.

What this does NOT change: the patch-09 latch. `mark_backend_down` /
`backend_down` / the BACKEND_STATE_TTL expiry are untouched, and the tests below
assert the patch-09 properties still hold.

Run with:  pytest test_ai_probe_concurrency.py
"""
import threading

import pytest

from sqldoc import ai


@pytest.fixture(autouse=True)
def clean_state():
    ai.reset_backend_state()
    yield
    ai.reset_backend_state()


class CountingProbe:
    """Stands in for the network call, counting how many threads reach it."""

    def __init__(self, delay=0.05):
        self.calls = 0
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.calls += 1
        # Hold the window open so a stampede is deterministic, not timing luck.
        threading.Event().wait(self.delay)
        raise OSError("probe target closed")


def _race(fn, n):
    """Run fn() on n threads released simultaneously."""
    barrier = threading.Barrier(n)
    errors = []

    def go():
        try:
            barrier.wait()
            fn()
        except Exception as e:          # pragma: no cover - surfaced by assert
            errors.append(repr(e))

    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


# --- the fix ---------------------------------------------------------------

@pytest.mark.parametrize("n", [2, 4, 16])
def test_concurrent_probe_costs_exactly_one_check(monkeypatch, n):
    """N threads probing the same backend at once must make ONE real probe."""
    probe = CountingProbe()
    monkeypatch.setattr(ai.requests, "get", probe)
    _race(lambda: ai.probe_backend("local"), n)
    assert probe.calls == 1, f"{n} concurrent callers made {probe.calls} probes"


def test_concurrent_probe_returns_identical_result(monkeypatch):
    """Waiters get the winner's result, not a separately-computed one."""
    monkeypatch.setattr(ai.requests, "get", CountingProbe())
    results = []
    lock = threading.Lock()

    def probe():
        r = ai.probe_backend("local")
        with lock:
            results.append(r)

    _race(probe, 8)
    assert len(results) == 8
    assert len(set(results)) == 1, f"threads disagreed: {set(results)}"


def test_sequential_memo_still_holds(monkeypatch):
    """The pre-existing sequential memoisation must not regress."""
    probe = CountingProbe(delay=0)
    monkeypatch.setattr(ai.requests, "get", probe)
    for _ in range(16):
        ai.probe_backend("local")
    assert probe.calls == 1


def test_probe_lock_is_per_backend():
    """Distinct backends must not serialise behind one another."""
    a = ai._probe_lock("ollama")
    b = ai._probe_lock("anthropic")
    assert a is not b
    assert ai._probe_lock("ollama") is a          # stable across calls


def test_down_lock_not_held_across_the_probe(monkeypatch):
    """A worker asking backend_down() mid-probe must not block on _DOWN_LOCK.

    This is why the fix adds a second lock instead of widening _DOWN_LOCK.
    """
    entered = threading.Event()
    release = threading.Event()

    def slow_probe(*a, **kw):
        entered.set()
        release.wait(5)
        raise OSError("probe target closed")

    monkeypatch.setattr(ai.requests, "get", slow_probe)
    prober = threading.Thread(target=lambda: ai.probe_backend("local"))
    prober.start()
    assert entered.wait(5), "probe never started"
    answered = threading.Event()

    def reader():
        ai.backend_down("ollama")
        answered.set()

    threading.Thread(target=reader).start()
    assert answered.wait(2), "backend_down() blocked while a probe was in flight"
    release.set()
    prober.join(5)


# --- patch-09 properties that must survive the fix -------------------------

def test_latch_first_reason_wins_under_concurrency():
    ai.mark_backend_down("ollama", "http://first", "FIRST")
    _race(lambda: ai.mark_backend_down("ollama", "http://other", "OTHER"), 16)
    endpoint, reason = ai.backend_down("ollama")
    assert (endpoint, reason) == ("http://first", "FIRST")


def test_sibling_thread_never_destroys_a_live_latch():
    """The patch-09 invariant: no thread may clear state another is using."""
    ai.mark_backend_down("ollama", "http://a", "held-by-A")
    seen = []
    stop = threading.Event()

    def watcher():
        while not stop.is_set():
            seen.append(ai.backend_down("ollama") is not None)

    w = threading.Thread(target=watcher)
    w.start()
    _race(lambda: [ai.mark_backend_down("ollama", "http://b", "B") for _ in range(200)], 8)
    stop.set()
    w.join()
    assert all(seen), f"latch vanished mid-fan-out ({seen.count(False)} dropouts)"
    assert ai.backend_down("ollama")[1] == "held-by-A"


def test_ttl_expiry_still_clears_without_reset(monkeypatch):
    monkeypatch.setattr(ai, "BACKEND_STATE_TTL", 0.2)
    ai.mark_backend_down("ollama", "http://x", "short")
    assert ai.backend_down("ollama") is not None
    threading.Event().wait(0.3)
    assert ai.backend_down("ollama") is None
    assert "ollama" not in ai._DOWN, "expired entry not dropped"


def test_expired_memo_triggers_exactly_one_reprobe(monkeypatch):
    """After the TTL, the next cycle re-probes -- once, not once per thread."""
    probe = CountingProbe(delay=0.05)
    monkeypatch.setattr(ai.requests, "get", probe)
    monkeypatch.setattr(ai, "BACKEND_STATE_TTL", 0.2)
    ai.probe_backend("local")
    assert probe.calls == 1
    threading.Event().wait(0.3)
    _race(lambda: ai.probe_backend("local"), 8)
    assert probe.calls == 2, f"re-probe cost {probe.calls - 1} checks, expected 1"
