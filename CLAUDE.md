# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sqldoc` is a CLI that connects to a **SQL Server** database, extracts schema metadata, uses an LLM to generate plain-English descriptions of each table and column, and renders a single self-contained HTML documentation file.

## Project status & roadmap (as of 2026-07-09)

### Built & validated
- **Cloud model + `--model` wiring** — `_call_anthropic` model is configurable and defaults to `claude-haiku-4-5` (cheap/fast, fits the one-call-per-table/column workload); `--model` threads through both backends with per-mode defaults.
- **Privacy guardrails** — local-first by default, mode-aware `Privacy:` banner line, cloud confirmation prompt (with `--yes` bypass for CI). Data boundary: only schema metadata is ever sent off-network; row data is never read.
- **ER diagram** (`renderer.py` → `_build_er`) — self-contained SVG: schema-colored table boxes in a masonry layout, deduped bezier FK arrows, self-reference loops, legend, zoom controls.
- **Real-time search** — sticky search box filtering tables + columns, with column drill-down, empty-group collapse, and a live match count.
- **Repo hygiene** — `README.md`, `requirements.txt`, `.gitignore` (`venv/`, `.env`, `*.html`), committer email corrected across history, pushed to `github.com/htamber1/sqldoc`.

**Validated end-to-end:** the `--no-ai` path against a local `AdventureWorks2022` (71 tables / 6 schemas). Renderer output confirmed: 71 SVG boxes, 86 FK arrows, well-formed SVG XML, search wiring present.

**AI path validated end-to-end (2026-07-10):** both backends run clean against `AdventureWorks2022`.
- **Local (Ollama, `llama3.1:8b`)** — `--schemas HumanResources` (6 tables): 6/6 table descriptions + 40/40 column descriptions populated and coherent; ~38s wall-clock (serial, one blocking call per table/column, as documented).
- **Cloud (Anthropic, default `claude-haiku-4-5`)** — `--schemas dbo` (3 tables) with `--yes`: 3/3 table + 21/21 column descriptions, coherent; ~7s. Privacy banner, warning, and `--yes` bypass all fired as designed.

### Phase 2 — delivered & validated (2026-07-10)
- **Views, stored procedures, indexes** — `extractor.py` gained `View`, `StoredProcedure`, `Parameter`, `Index` dataclasses and `extract_views()`/`extract_procedures()`; indexes (key vs included columns) attach to each `Table`. Rendered as their own schema-grouped sections with collapsible SQL definitions, parameter tables, and per-table index tables; stats + search span all object types. Validated against `AdventureWorks2022` (71 tables / 20 views / 10 procs).
- **Concurrent AI enrichment** — `enrich_tables/views/procedures` build a flat list of independent per-object work units and run them through a `ThreadPoolExecutor` (shared, lazily-created Anthropic client; `--concurrency`, default 8). Measured 1m49s → 22s on a cloud run over one schema. AI prompts for views/procs are **metadata-only** (names + columns/params, never the SQL definition) — the cloud data boundary is unchanged; definitions are extracted and rendered **locally only**.
- **Config file** — `.sqldoc.yml` (or `--config PATH`) supplies any option; precedence is CLI flag > config > default. Connection flags are no longer Click-required (validated post-merge). Gitignored (may hold a password); ships `.sqldoc.example.yml`.
- **Renderer hardening** — switched to an autoescaping Jinja `Environment` so SQL definitions containing `<`/`>`/`&` render as text instead of corrupting the HTML.

### Pending / unvalidated
- **ER diagram browser QA** — auto-layout can produce crossing arrows on dense schemas (71 tables is a lot). Not yet eyeballed in a browser. Visual polish is explicitly deferred. The new Views/Procedures sections and index tables have also only been validated structurally (regex over the HTML), not eyeballed in a browser.
- No automated test suite (see Tests below — the `test_*.py` scripts are ad-hoc, not pytest).

### Roadmap
**Phase 1 — visual parity/lead vs. Redgate SQL Doc (DONE).** ER diagram + real-time search. Both shipped and validated (`--no-ai`).

**Phase 2 — deeper coverage + AI quality (first wave DONE, see above).**

_Agreed next (2026-07-10), building now:_
- **PDF export** — a print/PDF-friendly rendering of the documentation.
- **Markdown export** — for GitHub wikis (per-object or single-file `.md`).
- **Schema change detection** — diff two runs and highlight what changed (added/removed/altered tables, columns, indexes, views, procs). Flagship enterprise feature (Dataedo charges premium for this).

_Decisions:_
- **SQL definitions stay out of AI calls for now.** View/proc definitions are extracted + rendered locally but never sent to the model, holding the cloud data boundary at "names/types/keys/row counts." A future **`--include-definitions`** opt-in flag will let users trade the wider boundary for richer AI descriptions that read the definition body; it must update the `Privacy:` banner + cloud warning to state that definitions are being sent, and stay off by default.

_Deferred:_ more object types (constraints, computed columns, triggers), `ai.py` retry/backoff + optional description caching, ER layout polish (fewer columns / key-columns-only / connected-tables-only toggle), dark mode.

**Phase 3 — distribution (PROPOSED, not yet agreed).**
- JSON export; connection UX (connection-string / `.env`-driven credentials); `--dry-run` cloud cost estimate. (Config file — done in Phase 2.)
- Packaging: `pyproject.toml` + console entry point (`sqldoc` command); replace ad-hoc `test_*.py` with a real pytest suite.

> Phase 3 is a proposed direction synthesized from discussion, **not** confirmed scope — refine with the user before building.

## Running

There is no `setup.py`/`pyproject.toml` and no console-script entry point (a `requirements.txt` now exists). Run the CLI as a module from the repo root, using the checked-in venv:

```bash
venv/Scripts/python.exe -m sqldoc.cli --server <host> --database <db> --username <user> --password <pw> --output docs.html
```

Key options (see `sqldoc/cli.py`):
- `--mode local|cloud` — `local` (default) calls Ollama at `http://localhost:11434`; `cloud` calls the Anthropic API.
- `--model` — model name. **Default is `None` and resolved per-mode** in `main()`: `llama3.1:8b` for local, `claude-haiku-4-5` for cloud. This split exists so the local Ollama tag never leaks into a cloud API call. Explicitly passing `--model` overrides for whichever backend is active; it is threaded all the way through to `_call_anthropic(prompt, model)` / `_call_ollama(prompt, model)`.
- `--no-ai` — skip all LLM calls, emit schema-only docs. Use this to iterate on extraction/rendering without a running LLM (also the fastest smoke test of the CLI plumbing).
- `--schemas` — comma-separated schema allowlist; filtering happens in `cli.py` *after* full extraction, and applies to tables, views, and procedures alike.
- `--concurrency` — parallel AI calls during enrichment (1-64, default 8). Threaded to all three `enrich_*` functions, which run their per-object calls through a `ThreadPoolExecutor`.
- `--config` — path to a `.sqldoc.yml` (default `.sqldoc.yml` in cwd if present). Any option can be set there; an explicit CLI flag overrides the config, which overrides the built-in default. Connection flags are optional when supplied via config.
- `--yes` / `-y` — bypass the cloud confirmation prompt (below) for non-interactive/CI runs.

**Privacy posture — local-first by design.** Local mode is the default; nothing leaves the network unless `--mode cloud` is explicitly chosen. The banner prints a `Privacy:` line stating the egress posture every run. `--mode cloud` additionally prints a warning and blocks on an interactive `click.confirm` (defaults to "no") before any network call — `--yes` skips only the prompt, not the warning. Note the data boundary: **only schema metadata** (table/column names, types, keys, row counts, existing `MS_Description` text) is ever sent to Anthropic — the extractor queries only `sys.*` catalog views and never reads table row data.

`--mode cloud` needs `ANTHROPIC_API_KEY`, loaded from `.env` via `python-dotenv` (`load_dotenv()` runs at import in `cli.py`).

## Dependencies

Pinned in `requirements.txt`: `click`, `pyodbc`, `anthropic`, `jinja2`, `requests`, `python-dotenv`, `PyYAML` (also installed in the checked-in `venv/`). Extraction requires the **ODBC Driver 18 for SQL Server** to be installed on the host (see the `DRIVER=` string in `extractor.py`) — this is a system package, not a pip dependency.

## Tests

`test_*.py` in the repo root are **not** pytest tests — they are ad-hoc scripts hardcoded to a local `AdventureWorks2022` database (`localhost`, `sa`/`SqlDoc123!`) and, for two of them, a running Ollama. Run one directly, e.g. `venv/Scripts/python.exe test_render.py`. They require live infrastructure and will fail without it. Do not treat them as a CI-style suite.

## Architecture

A linear pipeline, one module per stage, orchestrated by `cli.py`:

1. **`extractor.py`** — the only DB-facing code. Defines the shared-currency dataclasses: `Table`, `Column`, `Index` (attached to a `Table`), `View`, `Parameter`, `StoredProcedure`. `extract_metadata()` queries `sys.*` catalog views: tables + row counts, a per-table `sys.columns` join for PK/FK/`extended_properties` (`MS_Description`), and a per-table `sys.indexes`/`sys.index_columns` query (grouping key vs included columns). `extract_views()` and `extract_procedures()` add views (with `sys.sql_modules.definition`) and procedures (with `sys.parameters`). Any real `MS_Description` is captured here — the AI stage only fills descriptions left empty.

2. **`ai.py`** — `enrich_tables()`/`enrich_views()`/`enrich_procedures()` mutate the objects in place, setting `.description`. Two backends behind a `mode` switch: `_call_ollama` (HTTP to localhost) and `_call_anthropic` (SDK, shared lazily-created client, default model `claude-haiku-4-5`). Each `enrich_*` builds a flat list of independent per-object work units and runs them through a `ThreadPoolExecutor` (`--concurrency`, default 8) — each unit writes only its own object's `.description`, so there is no cross-thread shared state. View/proc prompts are **metadata-only** (names + columns/params, never the SQL definition) to keep the cloud data boundary unchanged.

3. **`renderer.py`** — `render_html(database, tables, output, views, procedures)` groups each object type by schema and renders the single `HTML_TEMPLATE` via an **autoescaping** Jinja `Environment` (all CSS inlined) to one standalone file. No external assets. Autoescaping matters: view/proc definitions contain `<`/`>`/`&`.

The dataclasses flow through unchanged: extractor builds them → ai enriches them → renderer reads them. If you add a field, touch all three stages. If you add a new **object type**, also thread it through `cli.py` (extract → schema-filter → enrich → render).
