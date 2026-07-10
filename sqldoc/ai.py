import os
import requests
from anthropic import Anthropic
from sqldoc.extractor import Table, View, StoredProcedure

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
    client = Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

def enrich_tables(tables: list[Table], mode: str = "local", model: str = "llama3.1:8b") -> list[Table]:
    for i, table in enumerate(tables):
        print(f"  [{i+1}/{len(tables)}] {table.schema}.{table.name}")
        table.description = generate_table_description(table, mode, model)
        for col in table.columns:
            if not col.description:
                col.description = generate_column_description(table.name, col, mode, model)
    return tables

def enrich_views(views: list[View], mode: str = "local", model: str = "llama3.1:8b") -> list[View]:
    for i, view in enumerate(views):
        print(f"  [{i+1}/{len(views)}] {view.schema}.{view.name} (view)")
        view.description = generate_view_description(view, mode, model)
        for col in view.columns:
            if not col.description:
                col.description = generate_column_description(view.name, col, mode, model)
    return views

def enrich_procedures(procedures: list[StoredProcedure], mode: str = "local", model: str = "llama3.1:8b") -> list[StoredProcedure]:
    for i, proc in enumerate(procedures):
        print(f"  [{i+1}/{len(procedures)}] {proc.schema}.{proc.name} (proc)")
        proc.description = generate_procedure_description(proc, mode, model)
    return procedures