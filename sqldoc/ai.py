import os
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
from anthropic import Anthropic
from sqldoc.extractor import Table, View, StoredProcedure

DEFAULT_CONCURRENCY = 8

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
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False
        }
    )
    return response.json()["response"].strip()

def _call_anthropic(prompt: str, model: str = "claude-haiku-4-5") -> str:
    client = _get_anthropic_client()
    message = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

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


def enrich_tables(tables: list[Table], mode: str = "local", model: str = "llama3.1:8b",
                  concurrency: int = DEFAULT_CONCURRENCY) -> list[Table]:
    tasks = []
    for table in tables:
        tasks.append(lambda t=table: setattr(t, "description", generate_table_description(t, mode, model)))
        for col in table.columns:
            if not col.description:
                tasks.append(lambda tn=table.name, c=col: setattr(c, "description", generate_column_description(tn, c, mode, model)))
    _run_tasks(tasks, concurrency, "table")
    return tables

def enrich_views(views: list[View], mode: str = "local", model: str = "llama3.1:8b",
                 concurrency: int = DEFAULT_CONCURRENCY) -> list[View]:
    tasks = []
    for view in views:
        tasks.append(lambda v=view: setattr(v, "description", generate_view_description(v, mode, model)))
        for col in view.columns:
            if not col.description:
                tasks.append(lambda vn=view.name, c=col: setattr(c, "description", generate_column_description(vn, c, mode, model)))
    _run_tasks(tasks, concurrency, "view")
    return views

def enrich_procedures(procedures: list[StoredProcedure], mode: str = "local", model: str = "llama3.1:8b",
                      concurrency: int = DEFAULT_CONCURRENCY) -> list[StoredProcedure]:
    tasks = [lambda p=proc: setattr(p, "description", generate_procedure_description(p, mode, model))
             for proc in procedures]
    _run_tasks(tasks, concurrency, "procedure")
    return procedures