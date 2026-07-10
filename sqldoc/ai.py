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


def _retry(fn, what: str):
    """Call fn(), retrying transient failures with exponential backoff + jitter."""
    delay = 1.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
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

def _sig_table(t) -> str:
    cols = "|".join(f"{c.name}:{c.data_type}:{int(c.is_primary_key)}{int(c.is_foreign_key)}" for c in t.columns)
    return f"{t.schema}.{t.name}|{cols}"

def _sig_view(v) -> str:
    cols = "|".join(f"{c.name}:{c.data_type}" for c in v.columns)
    return f"{v.schema}.{v.name}|{cols}"

def _sig_proc(p) -> str:
    params = "|".join(f"{pm.name}:{pm.data_type}:{int(pm.is_output)}" for pm in p.parameters)
    return f"{p.schema}.{p.name}|{params}"

def _sig_col(container: str, col) -> str:
    return f"{container}.{col.name}:{col.data_type}"

def _key(model: str, kind: str, sig: str) -> str:
    return hashlib.sha1(f"{model}\x1f{kind}\x1f{sig}".encode("utf-8")).hexdigest()

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

def generate_table_description(table: Table, mode: str = "local", model: str = "llama3.1:8b") -> str:
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
{column_info}

Respond with only the description, no preamble."""

    if mode == "local":
        return _call_ollama(prompt, model)
    else:
        return _call_anthropic(prompt, model)

def generate_column_description(table_name: str, col, mode: str = "local", model: str = "llama3.1:8b") -> str:
    prompt = f"""In one sentence, describe what the column '{col.name}' ({col.data_type}) likely stores in the '{table_name}' table. Respond with only the description, no preamble."""

    if mode == "local":
        return _call_ollama(prompt, model)
    else:
        return _call_anthropic(prompt, model)

def generate_view_description(view: View, mode: str = "local", model: str = "llama3.1:8b") -> str:
    # Metadata only: name + column names/types. The view's SQL definition is
    # deliberately NOT sent to the AI, to keep the cloud data boundary limited
    # to schema metadata (the definition is rendered locally instead).
    column_info = "\n".join(f"  - {col.name} ({col.data_type})" for col in view.columns)
    prompt = f"""You are documenting a SQL Server view. Based on the view name, schema, and its output columns, write a clear 2-3 sentence description of what this view likely presents and its business purpose.

View: {view.schema}.{view.name}
Columns:
{column_info}

Respond with only the description, no preamble."""

    if mode == "local":
        return _call_ollama(prompt, model)
    else:
        return _call_anthropic(prompt, model)

def generate_procedure_description(proc: StoredProcedure, mode: str = "local", model: str = "llama3.1:8b") -> str:
    # Metadata only: name + parameter names/types/direction. The proc body is
    # deliberately NOT sent to the AI (rendered locally instead).
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
{param_info}

Respond with only the description, no preamble."""

    if mode == "local":
        return _call_ollama(prompt, model)
    else:
        return _call_anthropic(prompt, model)

def _call_ollama(prompt: str, model: str = "llama3.1:8b") -> str:
    def do():
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["response"].strip()
    return _retry(do, f"ollama:{model}")

def _call_anthropic(prompt: str, model: str = "claude-haiku-4-5") -> str:
    def do():
        client = _get_anthropic_client()
        message = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    return _retry(do, f"anthropic:{model}")

def _run_tasks(tasks: list, concurrency: int, label: str):
    """Run independent, zero-argument LLM-call tasks across a thread pool.

    Each task performs one blocking model call and writes its result onto its
    own target object's `.description`, so tasks never touch shared state and
    can run fully in parallel. A failed task logs and is skipped rather than
    aborting the whole run; progress is reported from a single locked counter.
    """
    if not tasks:
        return
    total = len(tasks)
    state = {"done": 0}
    lock = threading.Lock()

    def worker(fn):
        try:
            fn()
        except Exception as e:
            with lock:
                print(f"    ! {label} description failed: {e}")
        finally:
            with lock:
                state["done"] += 1
                d = state["done"]
            if d % 10 == 0 or d == total:
                print(f"  [{d}/{total}] {label} descriptions generated")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(worker, tasks))


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

def _report(label, tasks, stats, cache):
    if cache is not None:
        print(f"  {label}: {stats['hits']} reused from cache, {len(tasks)} generated")


def enrich_tables(tables: list[Table], mode: str = "local", model: str = "llama3.1:8b",
                  concurrency: int = DEFAULT_CONCURRENCY, cache: dict = None) -> list[Table]:
    tasks, stats = [], {"hits": 0}
    for table in tables:
        _cache_or_task(cache, _key(model, "table", _sig_table(table)), table,
                       (lambda t=table: generate_table_description(t, mode, model)), tasks, stats)
        for col in table.columns:
            if col.description:
                continue
            _cache_or_task(cache, _key(model, "column", _sig_col(table.name, col)), col,
                           (lambda tn=table.name, c=col: generate_column_description(tn, c, mode, model)), tasks, stats)
    _run_tasks(tasks, concurrency, "table")
    _report("tables", tasks, stats, cache)
    return tables

def enrich_views(views: list[View], mode: str = "local", model: str = "llama3.1:8b",
                 concurrency: int = DEFAULT_CONCURRENCY, cache: dict = None) -> list[View]:
    tasks, stats = [], {"hits": 0}
    for view in views:
        _cache_or_task(cache, _key(model, "view", _sig_view(view)), view,
                       (lambda v=view: generate_view_description(v, mode, model)), tasks, stats)
        for col in view.columns:
            if col.description:
                continue
            _cache_or_task(cache, _key(model, "column", _sig_col(view.name, col)), col,
                           (lambda vn=view.name, c=col: generate_column_description(vn, c, mode, model)), tasks, stats)
    _run_tasks(tasks, concurrency, "view")
    _report("views", tasks, stats, cache)
    return views

def enrich_procedures(procedures: list[StoredProcedure], mode: str = "local", model: str = "llama3.1:8b",
                      concurrency: int = DEFAULT_CONCURRENCY, cache: dict = None) -> list[StoredProcedure]:
    tasks, stats = [], {"hits": 0}
    for proc in procedures:
        _cache_or_task(cache, _key(model, "proc", _sig_proc(proc)), proc,
                       (lambda p=proc: generate_procedure_description(p, mode, model)), tasks, stats)
    _run_tasks(tasks, concurrency, "procedure")
    _report("procedures", tasks, stats, cache)
    return procedures