# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sqldoc` is a CLI that connects to a **SQL Server** database, extracts schema metadata, uses an LLM to generate plain-English descriptions of each table and column, and renders a single self-contained HTML documentation file.

## Running

There is no `setup.py`/`pyproject.toml` and no console-script entry point (a `requirements.txt` now exists). Run the CLI as a module from the repo root, using the checked-in venv:

```bash
venv/Scripts/python.exe -m sqldoc.cli --server <host> --database <db> --username <user> --password <pw> --output docs.html
```

Key options (see `sqldoc/cli.py`):
- `--mode local|cloud` — `local` (default) calls Ollama at `http://localhost:11434`; `cloud` calls the Anthropic API.
- `--model` — model name. **Default is `None` and resolved per-mode** in `main()`: `llama3.1:8b` for local, `claude-haiku-4-5` for cloud. This split exists so the local Ollama tag never leaks into a cloud API call. Explicitly passing `--model` overrides for whichever backend is active; it is threaded all the way through to `_call_anthropic(prompt, model)` / `_call_ollama(prompt, model)`.
- `--no-ai` — skip all LLM calls, emit schema-only docs. Use this to iterate on extraction/rendering without a running LLM (also the fastest smoke test of the CLI plumbing).
- `--schemas` — comma-separated schema allowlist; filtering happens in `cli.py` *after* full extraction.
- `--yes` / `-y` — bypass the cloud confirmation prompt (below) for non-interactive/CI runs.

**Privacy posture — local-first by design.** Local mode is the default; nothing leaves the network unless `--mode cloud` is explicitly chosen. The banner prints a `Privacy:` line stating the egress posture every run. `--mode cloud` additionally prints a warning and blocks on an interactive `click.confirm` (defaults to "no") before any network call — `--yes` skips only the prompt, not the warning. Note the data boundary: **only schema metadata** (table/column names, types, keys, row counts, existing `MS_Description` text) is ever sent to Anthropic — the extractor queries only `sys.*` catalog views and never reads table row data.

`--mode cloud` needs `ANTHROPIC_API_KEY`, loaded from `.env` via `python-dotenv` (`load_dotenv()` runs at import in `cli.py`).

## Dependencies

Pinned in `requirements.txt`: `click`, `pyodbc`, `anthropic`, `jinja2`, `requests`, `python-dotenv` (also installed in the checked-in `venv/`). Extraction requires the **ODBC Driver 18 for SQL Server** to be installed on the host (see the `DRIVER=` string in `extractor.py`) — this is a system package, not a pip dependency.

## Tests

`test_*.py` in the repo root are **not** pytest tests — they are ad-hoc scripts hardcoded to a local `AdventureWorks2022` database (`localhost`, `sa`/`SqlDoc123!`) and, for two of them, a running Ollama. Run one directly, e.g. `venv/Scripts/python.exe test_render.py`. They require live infrastructure and will fail without it. Do not treat them as a CI-style suite.

## Architecture

A linear pipeline, one module per stage, orchestrated by `cli.py`:

1. **`extractor.py`** — the only DB-facing code. Defines the `Table` and `Column` dataclasses that are the shared currency of the whole pipeline. `extract_metadata()` queries `sys.*` catalog views: one query for tables + row counts, then a per-table query joining `sys.columns` with PK/FK/`extended_properties` (`MS_Description`) info. Any real `MS_Description` on a column is captured into `Column.description` here — the AI stage only fills columns where this is empty.

2. **`ai.py`** — `enrich_tables()` mutates the `Table`/`Column` objects in place, setting `.description`. Two backends behind a `mode` switch: `_call_ollama` (HTTP to localhost) and `_call_anthropic` (SDK, default model `claude-haiku-4-5`). Both take the resolved `model` string threaded down from the CLI. It generates a table-level description for every table, and a column-level description only for columns lacking one. This stage does one blocking LLM call per table plus one per undocumented column — it is the slow part and scales linearly with schema size, which is why cloud mode defaults to the cheap/fast Haiku model.

3. **`renderer.py`** — `render_html()` groups tables by schema and renders the single `HTML_TEMPLATE` Jinja string (all CSS inlined) to one standalone file. No external assets.

The dataclasses flow through unchanged: extractor builds them → ai enriches them → renderer reads them. If you add a field, touch all three stages.
