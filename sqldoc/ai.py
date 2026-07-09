import os
import requests
from anthropic import Anthropic
from sqldoc.extractor import Table

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
        return _call_anthropic(prompt)

def generate_column_description(table_name: str, col, mode: str = "local", model: str = "llama3.1:8b") -> str:
    prompt = f"""In one sentence, describe what the column '{col.name}' ({col.data_type}) likely stores in the '{table_name}' table. Respond with only the description, no preamble."""

    if mode == "local":
        return _call_ollama(prompt, model)
    else:
        return _call_anthropic(prompt)

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

def _call_anthropic(prompt: str) -> str:
    client = Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
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