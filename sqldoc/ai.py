import os
import time
import json
import random
import hashlib
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
from anthropic import Anthropic
from sqldoc.extractor import Table, View, StoredProcedure

DEFAULT_CONCURRENCY = 8
MAX_ATTEMPTS = 4          # 1 try + 3 retries
CACHE_VERSION = 1

# One cheap reachability check per backend, per process, before any fan-out.
PROBE_TIMEOUT = 5.0

# How long a probe result and a down-latch stay valid.
#
# This TTL is what lets a long-lived host (the agent daemon) pick up a backend
# that has come back, WITHOUT anyone calling a destructive reset. That matters
# because the state is shared on purpose: enrich_*() fans out over a thread pool,
# and the latch only works if one worker's hard failure is visible to its
# siblings. Anything that clears the state globally -- as the agent poller used
# to do at the top of every cycle -- can wipe a latch that another database's
# poller thread is actively relying on, restoring the per-object retry storm for
# that database. Expiry has no such cross-thread edge.
#
# 60s is comfortably longer than a full degraded fan-out (~4s for 10,000 objects)
# so a latch cannot expire mid-run, and comfortably shorter than any sane poll
# interval so each cycle re-probes.
BACKEND_STATE_TTL = 60.0

def ollama_base_url() -> str:
    """The Ollama endpoint actually in use.

    Read per call (not at import) so the environment can change between runs and
    so tests can point the probe at a closed port. OLLAMA_HOST is the same
    variable the ollama CLI itself honours, and it accepts a bare host:port.
    """
    base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip().rstrip("/")
    if not base:
        base = "http://localhost:11434"
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base


class BackendUnavailable(RuntimeError):
    """The AI backend cannot be reached or used at all (refused connection,
    missing SDK, missing or rejected credentials).

    Distinct from a transient failure: retrying will not help, so nothing that
    raises this is retried, and the caller degrades instead of attempting the
    remaining objects one by one.
    """

    def __init__(self, backend: str, endpoint: str, reason: str):
        super().__init__(f"{backend} unavailable at {endpoint}: {reason}")
        self.backend = backend
        self.endpoint = endpoint
        self.reason = reason


def backend_endpoint(backend: str) -> str:
    """Human-readable endpoint for a backend, for notices and error text."""
    if backend == "ollama":
        return ollama_base_url()
    return {
        "anthropic": "api.anthropic.com (ANTHROPIC_API_KEY)",
        "openai": "api.openai.com (OPENAI_API_KEY)",
        "gemini": "generativelanguage.googleapis.com (GOOGLE_API_KEY)",
    }.get(backend, backend)


# --- Backend availability: probe once, latch, never retry a dead backend ----
# Two mechanisms, deliberately both:
#   * probe_backend() is the up-front check a command makes before fanning out;
#   * the _DOWN latch is the backstop for any path that does NOT probe -- the
#     first hard failure marks the backend down and every later call fails fast,
#     instead of repeating the same refused connection thousands of times.
#
# Both are keyed by BACKEND and shared process-wide, deliberately: the fan-out
# runs on a thread pool, so a worker's hard failure has to be visible to its
# siblings for the latch to bound anything. Entries carry an expiry instead of
# being cleared, so a long-lived process recovers without any thread having to
# destroy state another thread is using.
_DOWN = {}
_DOWN_LOCK = threading.Lock()
_PROBED = {}

# One lock per backend, held only across an actual probe. _DOWN_LOCK guards the
# dicts and must never be held across a network call (a worker asking
# backend_down() mid-fan-out would block for PROBE_TIMEOUT); this second, much
# narrower lock is what makes the _PROBED memo hold when several poller threads
# probe the same backend at the same instant. See probe_backend().
_PROBE_LOCKS = {}
_PROBE_LOCKS_LOCK = threading.Lock()


def _probe_lock(backend: str):
    """The lock for `backend`, created on first use."""
    with _PROBE_LOCKS_LOCK:
        lock = _PROBE_LOCKS.get(backend)
        if lock is None:
            lock = _PROBE_LOCKS[backend] = threading.Lock()
        return lock


def _read_probe_memo(backend: str):
    """The memoised probe result for `backend`, or None. Drops it if expired."""
    with _DOWN_LOCK:
        cached = _PROBED.get(backend)
        if cached is not None and cached[1] <= _now():
            del _PROBED[backend]
            cached = None
    return cached[0] if cached is not None else None


def _now() -> float:
    return time.monotonic()


def mark_backend_down(backend: str, endpoint: str, reason: str):
    """Latch a backend as unusable (first reason wins until the entry expires)."""
    with _DOWN_LOCK:
        cur = _DOWN.get(backend)
        if cur is None or cur[2] <= _now():
            _DOWN[backend] = (endpoint, reason, _now() + BACKEND_STATE_TTL)


def backend_down(backend: str):
    """(endpoint, reason) if this backend is known-dead right now, else None.

    An entry past its TTL is treated as absent (and dropped), so the next caller
    re-probes a backend that may have come back.
    """
    with _DOWN_LOCK:
        entry = _DOWN.get(backend)
        if entry is None:
            return None
        endpoint, reason, expires = entry
        if expires <= _now():
            del _DOWN[backend]
            return None
        return (endpoint, reason)


def degraded() -> bool:
    """True if any backend is currently latched down, so a caller that never
    probed can still report honestly at the end of a run."""
    with _DOWN_LOCK:
        now = _now()
        return any(exp > now for _, _, exp in _DOWN.values())


def degraded_detail():
    """(backend, endpoint, reason) for a backend currently down, else None."""
    with _DOWN_LOCK:
        now = _now()
        for backend, (endpoint, reason, exp) in _DOWN.items():
            if exp > now:
                return (backend, endpoint, reason)
    return None


def reset_backend_state():
    """Clear probe + latch state outright.

    For TESTS, and for a process that genuinely wants to start clean. Do NOT
    call this from a worker or a per-database poll cycle: the state is shared
    across threads on purpose, and clearing it there wipes a latch another
    thread's fan-out is relying on. Long-lived hosts should let
    BACKEND_STATE_TTL expire the entries instead.
    """
    with _DOWN_LOCK:
        _DOWN.clear()
        _PROBED.clear()


def _is_unreachable(exc) -> bool:
    """True for failures that will never succeed on retry: refused or unroutable
    connections, a missing optional SDK, and absent or rejected credentials.

    Matched on type NAME rather than by importing the optional cloud SDKs, so
    this stays correct whether or not openai / gemini are installed.
    """
    if isinstance(exc, BackendUnavailable):
        return True
    if isinstance(exc, ImportError):
        return True
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.ConnectTimeout)):
        return True
    if type(exc).__name__ in (
            "APIConnectionError", "APIConnectionTimeoutError", "ConnectError",
            "ConnectionRefusedError", "AuthenticationError",
            "PermissionDeniedError", "NotFoundError"):
        return True
    # requests HTTPError: 401/403/404 are permanent; 5xx and 429 stay transient.
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code in (401, 403, 404)


def probe_backend(mode: str = "local", backend: str = None, model: str = None,
                  timeout: float = None) -> tuple:
    """Check ONCE whether the effective AI backend can be used at all.

    Returns (ok, endpoint, reason). Memoized per backend per process, so calling
    it from several commands -- or twice in one command -- costs one check. A
    negative result also latches the backend down, so any AI call that slips
    past the preflight fails fast rather than retrying.

    Backend-agnostic by construction: each backend answers the same question
    ("can I use you at all?") the cheapest way it can -- a local metadata GET
    for Ollama, a credential/SDK check for the cloud backends. Never a billable
    call, and never a model load.
    """
    backend = resolve_backend(mode, backend)
    endpoint = backend_endpoint(backend)
    cached = _read_probe_memo(backend)
    if cached is not None:
        return cached
    timeout = PROBE_TIMEOUT if timeout is None else timeout

    # Serialise the probe itself per backend. Reading the memo, releasing the
    # lock and only then probing means N threads arriving together all miss and
    # all probe -- run_daemon starts every poller thread at once and each polls
    # immediately, so that is the normal shape of cycle 1, not a rare race. The
    # second read below is the "checked" half: whoever waited here gets the
    # winner's result instead of repeating the call.
    with _probe_lock(backend):
        cached = _read_probe_memo(backend)
        if cached is not None:
            return cached
        return _do_probe(backend, endpoint, timeout)


def _do_probe(backend: str, endpoint: str, timeout: float) -> tuple:
    """Perform one real probe and record it. Caller holds that backend's probe
    lock, so exactly one thread per backend is ever in here."""
    if backend == "ollama":
        try:
            resp = requests.get(ollama_base_url() + "/api/tags", timeout=timeout)
            resp.raise_for_status()
            ok, reason = True, "reachable"
        except Exception as e:
            ok, reason = False, f"{type(e).__name__}: {e}"
    elif backend == "anthropic":
        ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        reason = "reachable" if ok else "ANTHROPIC_API_KEY is not set"
    elif backend == "openai":
        ok = bool(os.environ.get("OPENAI_API_KEY"))
        reason = "reachable" if ok else "OPENAI_API_KEY is not set"
        if ok:
            try:
                import openai  # noqa: F401
            except ImportError:
                ok, reason = False, ("the 'openai' package is not installed "
                                     "(pip install sqldoc[openai])")
    elif backend == "gemini":
        ok = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
        reason = "reachable" if ok else "GOOGLE_API_KEY / GEMINI_API_KEY is not set"
        if ok:
            try:
                import google.generativeai  # noqa: F401
            except ImportError:
                ok, reason = False, ("the 'google-generativeai' package is not installed "
                                     "(pip install sqldoc[gemini])")
    else:
        ok, reason = False, f"unknown AI backend '{backend}'"

    result = (ok, endpoint, reason)
    with _DOWN_LOCK:
        _PROBED[backend] = (result, _now() + BACKEND_STATE_TTL)
    if not ok:
        mark_backend_down(backend, endpoint, reason)
    return result


def _retry(fn, what: str, backend: str = None):
    """Call fn(), retrying TRANSIENT failures with exponential backoff + jitter.

    Two things this deliberately does not do:

    * It does not retry an unreachable backend. A refused connection, a missing
      SDK or a rejected key fails the same way every time, so retrying it three
      times with backoff just multiplies the wait by four for no chance of
      success. Those raise :class:`BackendUnavailable` on the first attempt.
    * It does not re-attempt a backend already known to be down. The first hard
      failure latches it, and every later call returns immediately. Without this,
      a per-object fan-out (one task per table AND per column) repeats the same
      dead connection thousands of times -- which is how a schema-only run came
      to take hours instead of seconds.
    """
    backend = backend or (what.split(":", 1)[0] if what else "ollama")
    down = backend_down(backend)
    if down is not None:
        endpoint, reason = down
        raise BackendUnavailable(backend, endpoint, reason)

    delay = 1.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
            if _is_unreachable(e):
                endpoint = backend_endpoint(backend)
                reason = f"{type(e).__name__}: {e}"
                mark_backend_down(backend, endpoint, reason)
                raise BackendUnavailable(backend, endpoint, reason) from e
            if attempt == MAX_ATTEMPTS:
                raise
            wait = delay + random.uniform(0, 0.4)
            print(f"    retry {attempt}/{MAX_ATTEMPTS - 1} for {what}: {type(e).__name__}: {e} (waiting {wait:.1f}s)")
            time.sleep(wait)
            delay *= 2


# --- Description cache -----------------------------------------------------
# Descriptions are keyed by (model, kind, structural signature). If an object's
# structure is unchanged since the last run, its description is reused instead of
# calling the LLM again — saving cost and making incremental runs fast.

def _def_sig(*definitions) -> str:
    """A short digest of one or more SQL bodies, folded into a cache signature
    when --include-definitions is on so a changed body invalidates the cache."""
    joined = "\x1e".join(d for d in definitions if d)
    if not joined:
        return ""
    # Non-security cache key (structural signature). SHA-256 (usedforsecurity=False
    # documents that this is a content fingerprint, not a signature).
    return "|def:" + hashlib.sha256(joined.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

def _sig_table(t, include_definitions=False) -> str:
    cols = "|".join(f"{c.name}:{c.data_type}:{int(c.is_primary_key)}{int(c.is_foreign_key)}" for c in t.columns)
    sig = f"{t.schema}.{t.name}|{cols}"
    if include_definitions:
        sig += _def_sig(*(tg.definition for tg in t.triggers))
    return sig

def _sig_view(v, include_definitions=False) -> str:
    cols = "|".join(f"{c.name}:{c.data_type}" for c in v.columns)
    sig = f"{v.schema}.{v.name}|{cols}"
    if include_definitions:
        sig += _def_sig(v.definition)
    return sig

def _sig_proc(p, include_definitions=False) -> str:
    params = "|".join(f"{pm.name}:{pm.data_type}:{int(pm.is_output)}" for pm in p.parameters)
    sig = f"{p.schema}.{p.name}|{params}"
    if include_definitions:
        sig += _def_sig(p.definition)
    return sig

def _sig_col(container: str, col) -> str:
    return f"{container}.{col.name}:{col.data_type}"

def _key(model: str, kind: str, sig: str) -> str:
    # Non-security cache key (content fingerprint, not a signature).
    return hashlib.sha256(f"{model}\x1f{kind}\x1f{sig}".encode("utf-8"),
                          usedforsecurity=False).hexdigest()

def load_cache(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {"version": CACHE_VERSION, "entries": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        data = {"version": CACHE_VERSION, "entries": {}}
    return data

def save_cache(cache: dict, path: str):
    if cache is None or not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)

# One shared Anthropic client, created lazily. The SDK client is thread-safe and
# reuses its connection pool, so sharing it across worker threads is both correct
# and faster than constructing a client per call.
_anthropic_client = None
_anthropic_lock = threading.Lock()

def _get_anthropic_client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        with _anthropic_lock:
            if _anthropic_client is None:
                _anthropic_client = Anthropic()
    return _anthropic_client

def generate_table_description(table: Table, mode: str = "local", model: str = "llama3.1:8b",
                               include_definitions: bool = False) -> str:
    column_info = "\n".join([
        f"  - {col.name} ({col.data_type})"
        f"{'[PK]' if col.is_primary_key else ''}"
        f"{'[FK -> ' + col.references_table + ']' if col.is_foreign_key else ''}"
        f"{': ' + col.description if col.description else ''}"
        for col in table.columns
    ])

    prompt = f"""You are documenting a SQL Server database table. Based on the table name, schema, and column names, write a clear 2-3 sentence description of what this table likely stores and its business purpose.

Table: {table.schema}.{table.name}
Row count: {table.row_count}
Columns:
{column_info}"""

    # Opt-in: include trigger bodies so the AI can reason about side effects.
    if include_definitions:
        trig = "\n\n".join(f"-- trigger {tg.name}\n{tg.definition}"
                           for tg in table.triggers if tg.definition)
        if trig:
            prompt += f"\n\nTrigger definitions:\n{trig}"

    prompt += "\n\nRespond with only the description, no preamble."

    return dispatch(prompt, mode, model)

def generate_column_description(table_name: str, col, mode: str = "local", model: str = "llama3.1:8b") -> str:
    prompt = f"""In one sentence, describe what the column '{col.name}' ({col.data_type}) likely stores in the '{table_name}' table. Respond with only the description, no preamble."""

    return dispatch(prompt, mode, model)

def generate_view_description(view: View, mode: str = "local", model: str = "llama3.1:8b",
                              include_definitions: bool = False) -> str:
    # Metadata only by default: name + column names/types. The view's SQL
    # definition is NOT sent unless --include-definitions is set, keeping the
    # default cloud data boundary limited to schema metadata (the definition is
    # rendered locally regardless).
    column_info = "\n".join(f"  - {col.name} ({col.data_type})" for col in view.columns)
    prompt = f"""You are documenting a SQL Server view. Based on the view name, schema, and its output columns, write a clear 2-3 sentence description of what this view likely presents and its business purpose.

View: {view.schema}.{view.name}
Columns:
{column_info}"""

    if include_definitions and view.definition:
        prompt += f"\n\nSQL definition:\n{view.definition}"

    prompt += "\n\nRespond with only the description, no preamble."

    return dispatch(prompt, mode, model)

def generate_procedure_description(proc: StoredProcedure, mode: str = "local", model: str = "llama3.1:8b",
                                   include_definitions: bool = False) -> str:
    # Metadata only by default: name + parameter names/types/direction. The proc
    # body is NOT sent unless --include-definitions is set (rendered locally
    # regardless).
    if proc.parameters:
        param_info = "\n".join(
            f"  - {p.name} ({p.data_type}){' OUTPUT' if p.is_output else ''}"
            for p in proc.parameters
        )
    else:
        param_info = "  (no parameters)"
    prompt = f"""You are documenting a SQL Server stored procedure. Based on the procedure name, schema, and its parameters, write a clear 2-3 sentence description of what this procedure likely does and its business purpose.

Procedure: {proc.schema}.{proc.name}
Parameters:
{param_info}"""

    if include_definitions and proc.definition:
        prompt += f"\n\nSQL definition:\n{proc.definition}"

    prompt += "\n\nRespond with only the description, no preamble."

    return dispatch(prompt, mode, model)

# --- AI backends -----------------------------------------------------------
# Four interchangeable backends behind one prompt interface. `ollama` is local;
# `anthropic`/`openai`/`gemini` are cloud. Which one runs is chosen by
# resolve_backend(): an explicit --ai-backend (recorded via set_backend) wins,
# otherwise it derives from --mode (local->ollama, cloud->anthropic) so existing
# behaviour is unchanged. Each backend has a sensible default model.

CLOUD_BACKENDS = {"anthropic", "openai", "gemini"}
ALL_BACKENDS = ("ollama", "anthropic", "openai", "gemini")

BACKEND_MODELS = {
    "ollama": "llama3.1:8b",
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o",
    "gemini": "gemini-1.5-flash",
}

# Process-wide backend override, set once from --ai-backend so downstream
# per-command AI helpers (insights, waits, plans, deadlocks, the agent) all
# route to the chosen backend without threading it through every signature.
_ACTIVE_BACKEND = None


def set_backend(name):
    """Record the --ai-backend choice for the process (None clears it)."""
    global _ACTIVE_BACKEND
    if name and name not in ALL_BACKENDS:
        raise ValueError(f"Unknown AI backend '{name}' (choose from {', '.join(ALL_BACKENDS)}).")
    _ACTIVE_BACKEND = name or None


# Process-wide industry guidance, set once from --industry so every AI prompt
# (table/column/view/proc descriptions, glossary, NL-to-SQL) is framed for the
# chosen vertical without threading it through every enrich signature.
_INDUSTRY_GUIDANCE = ""


def set_industry_guidance(text):
    """Record the --industry AI guidance for the process ('' clears it)."""
    global _INDUSTRY_GUIDANCE
    _INDUSTRY_GUIDANCE = text or ""


def resolve_backend(mode="local", backend=None):
    """Pick the backend: explicit arg > --ai-backend override > derived from mode."""
    if backend:
        return backend
    if _ACTIVE_BACKEND:
        return _ACTIVE_BACKEND
    return "ollama" if mode == "local" else "anthropic"


def default_model(backend):
    return BACKEND_MODELS.get(backend, "llama3.1:8b")


def is_cloud_backend(mode="local", backend=None):
    """True when the effective backend sends prompts off-network (drives the
    privacy banner + cloud-confirm prompt)."""
    return resolve_backend(mode, backend) in CLOUD_BACKENDS


def dispatch(prompt: str, mode: str = "local", model: str = None,
             backend: str = None, max_tokens: int = 200) -> str:
    """Call the effective AI backend with a single prompt and return its text.
    The one entry point every AI feature funnels through."""
    backend = resolve_backend(mode, backend)
    if _INDUSTRY_GUIDANCE:
        prompt = f"{_INDUSTRY_GUIDANCE}\n\n{prompt}"
    if not model or (backend != "ollama" and model in BACKEND_MODELS.values() and model != BACKEND_MODELS[backend]):
        # No model given, or a different backend's default model leaked through
        # (mode-derived defaulting) — use this backend's own default.
        model = default_model(backend)
    if backend == "ollama":
        return _call_ollama(prompt, model)
    if backend == "anthropic":
        return _call_anthropic(prompt, model, max_tokens)
    if backend == "openai":
        return _call_openai(prompt, model, max_tokens)
    if backend == "gemini":
        return _call_gemini(prompt, model, max_tokens)
    raise ValueError(f"Unknown AI backend '{backend}'.")


def _call_ollama(prompt: str, model: str = "llama3.1:8b") -> str:
    def do():
        response = requests.post(
            ollama_base_url() + "/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["response"].strip()
    return _retry(do, f"ollama:{model}", backend="ollama")

def _call_anthropic(prompt: str, model: str = "claude-haiku-4-5", max_tokens: int = 200) -> str:
    def do():
        client = _get_anthropic_client()
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    return _retry(do, f"anthropic:{model}", backend="anthropic")


# OpenAI + Gemini clients are created lazily and shared (both SDKs are
# thread-safe and pool connections). The SDKs are optional dependencies —
# install with `pip install sqldoc[openai]` / `sqldoc[gemini]`.
_openai_client = None
_openai_lock = threading.Lock()
_gemini_configured = False
_gemini_lock = threading.Lock()


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        with _openai_lock:
            if _openai_client is None:
                try:
                    from openai import OpenAI
                except ImportError:
                    raise ImportError(
                        "The OpenAI backend needs the 'openai' package. "
                        "Install it with: pip install sqldoc[openai]")
                # Reads OPENAI_API_KEY from the environment.
                _openai_client = OpenAI()
    return _openai_client


def _call_openai(prompt: str, model: str = "gpt-4o", max_tokens: int = 300) -> str:
    def do():
        client = _get_openai_client()
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.choices[0].message.content or "").strip()
    return _retry(do, f"openai:{model}", backend="openai")


def _call_gemini(prompt: str, model: str = "gemini-1.5-flash", max_tokens: int = 300) -> str:
    def do():
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError(
                "The Gemini backend needs the 'google-generativeai' package. "
                "Install it with: pip install sqldoc[gemini]")
        global _gemini_configured
        if not _gemini_configured:
            with _gemini_lock:
                if not _gemini_configured:
                    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
                    genai.configure(api_key=api_key)
                    _gemini_configured = True
        gm = genai.GenerativeModel(model)
        resp = gm.generate_content(
            prompt, generation_config={"max_output_tokens": max_tokens})
        return (resp.text or "").strip()
    return _retry(do, f"gemini:{model}", backend="gemini")

def _run_tasks(tasks: list, concurrency: int, label: str):
    """Run independent, zero-argument LLM-call tasks across a thread pool.

    Each task performs one blocking model call and writes its result onto its
    own target object's `.description`, so tasks never touch shared state and
    can run fully in parallel. A failed task logs and is skipped rather than
    aborting the whole run; progress is reported from a single locked counter.
    """
    stats = {"total": len(tasks), "ok": 0, "failed": 0, "unavailable": 0, "skipped": 0}
    if not tasks:
        return stats
    total = len(tasks)
    state = {"done": 0}
    lock = threading.Lock()

    def worker(fn):
        try:
            fn()
            with lock:
                stats["ok"] += 1
        except BackendUnavailable as e:
            # The backend is gone. Count it and move on without printing per
            # object -- one notice for the run is the point of the latch.
            with lock:
                stats["unavailable"] += 1
                if stats["unavailable"] == 1:
                    print(f"    ! AI backend unavailable ({e.reason}); "
                          f"skipping the remaining {label} descriptions")
        except Exception as e:
            with lock:
                stats["failed"] += 1
                print(f"    ! {label} description failed: {e}")
        finally:
            with lock:
                state["done"] += 1
                d = state["done"]
                ok = stats["ok"]
            if d % 10 == 0 or d == total:
                # Report what was actually GENERATED, not merely attempted. The
                # old line said "[6/6] descriptions generated" after six
                # failures, which read as success in the logs.
                print(f"  [{d}/{total}] {label} descriptions attempted, {ok} generated")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(worker, tasks))
    return stats


def _cache_or_task(cache, key, target, genfn, tasks, stats):
    """If a cached description exists for key, apply it and count a hit; else
    queue a task that generates the description and writes it back to cache."""
    cached = cache["entries"].get(key) if cache is not None else None
    if cached is not None:
        target.description = cached
        stats["hits"] += 1
        return
    def task():
        val = genfn()
        target.description = val
        if cache is not None:
            cache["entries"][key] = val
    tasks.append(task)

def _report(label, tasks, stats, cache, run_stats=None):
    generated = run_stats["ok"] if run_stats else len(tasks)
    if cache is not None:
        print(f"  {label}: {stats['hits']} reused from cache, {generated} generated")
    if run_stats and (run_stats["unavailable"] or run_stats["failed"]):
        missed = run_stats["unavailable"] + run_stats["failed"]
        print(f"  {label}: {missed} of {run_stats['total']} left undescribed "
              f"(AI backend unavailable)" if run_stats["unavailable"]
              else f"  {label}: {missed} of {run_stats['total']} left undescribed")


def enrich_tables(tables: list[Table], mode: str = "local", model: str = "llama3.1:8b",
                  concurrency: int = DEFAULT_CONCURRENCY, cache: dict = None,
                  include_definitions: bool = False, stats_out: dict = None) -> list[Table]:
    tasks, stats = [], {"hits": 0}
    for table in tables:
        _cache_or_task(cache, _key(model, "table", _sig_table(table, include_definitions)), table,
                       (lambda t=table: generate_table_description(t, mode, model, include_definitions)), tasks, stats)
        for col in table.columns:
            if col.description:
                continue
            _cache_or_task(cache, _key(model, "column", _sig_col(table.name, col)), col,
                           (lambda tn=table.name, c=col: generate_column_description(tn, c, mode, model)), tasks, stats)
    run_stats = _run_tasks(tasks, concurrency, "table")
    _report("tables", tasks, stats, cache, run_stats)
    if stats_out is not None:
        for k, v in run_stats.items():
            stats_out[k] = stats_out.get(k, 0) + v
    return tables

def enrich_views(views: list[View], mode: str = "local", model: str = "llama3.1:8b",
                 concurrency: int = DEFAULT_CONCURRENCY, cache: dict = None,
                 include_definitions: bool = False, stats_out: dict = None) -> list[View]:
    tasks, stats = [], {"hits": 0}
    for view in views:
        _cache_or_task(cache, _key(model, "view", _sig_view(view, include_definitions)), view,
                       (lambda v=view: generate_view_description(v, mode, model, include_definitions)), tasks, stats)
        for col in view.columns:
            if col.description:
                continue
            _cache_or_task(cache, _key(model, "column", _sig_col(view.name, col)), col,
                           (lambda vn=view.name, c=col: generate_column_description(vn, c, mode, model)), tasks, stats)
    run_stats = _run_tasks(tasks, concurrency, "view")
    _report("views", tasks, stats, cache, run_stats)
    if stats_out is not None:
        for k, v in run_stats.items():
            stats_out[k] = stats_out.get(k, 0) + v
    return views

def enrich_procedures(procedures: list[StoredProcedure], mode: str = "local", model: str = "llama3.1:8b",
                      concurrency: int = DEFAULT_CONCURRENCY, cache: dict = None,
                      include_definitions: bool = False, stats_out: dict = None) -> list[StoredProcedure]:
    tasks, stats = [], {"hits": 0}
    for proc in procedures:
        _cache_or_task(cache, _key(model, "proc", _sig_proc(proc, include_definitions)), proc,
                       (lambda p=proc: generate_procedure_description(p, mode, model, include_definitions)), tasks, stats)
    run_stats = _run_tasks(tasks, concurrency, "procedure")
    _report("procedures", tasks, stats, cache, run_stats)
    if stats_out is not None:
        for k, v in run_stats.items():
            stats_out[k] = stats_out.get(k, 0) + v
    return procedures