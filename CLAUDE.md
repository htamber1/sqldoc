# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sqldoc` is a CLI that connects to a **SQL Server** database, extracts schema metadata, uses an LLM to generate plain-English descriptions of each table and column, and renders a single self-contained HTML documentation file. As of v1.1 it also ships a **PII / compliance scanner** (`sqldoc scan`), and post-1.2 a **database health analyzer** (`sqldoc health`, DMV-based). The CLI is a command group: **`sqldoc doc`** (documentation), **`sqldoc scan`** (PII scan), and **`sqldoc health`** (DMV health); a `DefaultGroup` routes `sqldoc <options>` (no subcommand) to `doc` for backward compatibility. Entry point is `sqldoc.cli:cli`.

## Project status (v1.2.0, as of 2026-07-10)

Shippable two-in-one CLI — **`sqldoc doc`** (documentation) and **`sqldoc scan`** (PII/compliance). Tags **v1.0.0 / v1.1.0 / v1.2.0** pushed to `github.com/htamber1/sqldoc`. **79 pytest tests** (mocked — no live SQL Server/Ollama). Validated end-to-end against a local `AdventureWorks2022` (71 tables / 20 views / 10 procs / 10 triggers / 10 computed columns; `sa`/`SqlDoc123!`).

### What's built (all shipped + tested)
**`sqldoc doc`** — `extractor.py` (tables, columns incl. PK/FK/**computed**, indexes, views+procs with definitions, **triggers**; single connection string via `build_connection_string()` or `--connection-string`) → `ai.py` (local Ollama / cloud Anthropic; `--concurrency`; retry+backoff; structural **description cache** `--cache`; metadata-only prompts) → renderers: **HTML** (`renderer.py` — dark theme, sidebar nav tree, interactive ER diagram, type filter+search, Copy SQL, color-coded row counts), **Markdown** (`markdown_renderer.py`), **PDF** (`pdf_renderer.py`/fpdf2); `--format`/extension dispatch. **Schema change detection** (`snapshot.py`, `--snapshot`).

**`sqldoc scan`** — `pii.py` (~21 PII categories → HIGH/MEDIUM/LOW + HIPAA/GDPR/PCI-DSS + action; camelCase-aware matcher; type confirmation; numeric confidence score + `--confidence-threshold`; per-column `pii_allowlist:`; optional AI `--sample` with values never stored; **custom categories** via `.sqldoc.yml` `pii_patterns:`) → `pii_renderer.py` (dark compliance HTML: dashboard, risk filter, CSV export). **PII drift** (`--baseline`), **SARIF export** (`sarif.py`, `--sarif`), **JSON** (`--json`), **CI gate** (`--fail-on high|new-high`).

**`sqldoc health`** — `health.py` (four DMV checks: slow queries `sys.dm_exec_query_stats`, dead tables `sys.dm_db_index_usage_stats`, missing indexes `sys.dm_db_missing_index_details` with generated `CREATE INDEX`, index fragmentation `sys.dm_db_index_physical_stats`; each check isolated so a missing `VIEW SERVER STATE` degrades that section only; reads statistics, never row data) → `health_renderer.py` (dark HTML dashboard + `build_health_json` for `--json`). Flags: `--top`, `--min-fragmentation`, `--min-pages`, `--schemas`.

**JSON export** — `json_renderer.py` (`sqldoc doc --format json` / `.json` extension, full model via `dataclasses.asdict`) and machine-readable findings for `scan --json` / `health --json`.

**Infra** — `pyproject.toml` + `sqldoc` console entry point (group via `DefaultGroup`; bare `sqldoc <opts>` → `doc`); pytest suite + `tests/conftest.py` fake-pyodbc; GitHub Actions CI (`.github/workflows/ci.yml`); `PUBLISHING.md`; `pricing-strategy.md`; `CHANGELOG.md`.

### Outstanding manual steps (need the user's credentials — see PUBLISHING.md)
1. **Push `.github/workflows/ci.yml`** — the git PAT lacks the `workflow` scope, so the file is on disk (untracked) but not pushed. Add the scope + push, or paste via the GitHub web UI. Contains a `test` job (pytest) + a guarded `pii-gate` job.
2. **Publish to PyPI** — builds + `twine check` pass, name `sqldoc` is free. Follow `PUBLISHING.md`. Decide first: (a) a license, (b) public PyPI vs. the paid tiers.
3. **GitHub Release pages** for the three tags (optional; the tags themselves exist).

### Next session — planned features
- **Entitlement layer** (unblocks paid tiers + public PyPI): license-key gating for `scan`, audit logs, air-gapped-mode validation.
- **JSON export** — `--format json` (doc) + machine-readable findings (scan) for programmatic consumers.
- **Constraints** (check/unique/default, FK actions) — the last object type not yet extracted; **ER layout toggles** (key-columns-only / connected-only).
- **`--include-definitions`** opt-in — send view/proc/trigger bodies to the AI for richer descriptions (must update the `Privacy:` banner + cloud warning; off by default).
- **Scan depth** — more PII categories + false-positive tuning; a confidence-threshold flag; per-column suppression/allowlist so known-safe columns don't re-alert.
- **`--dry-run` cloud cost estimate**; `.env`-driven credentials.

### Standing decisions
- SQL definitions stay **out of AI calls by default** (metadata-only cloud boundary). The opt-in **`--include-definitions`** flag (shipped, post-1.2.0) sends view/proc/trigger bodies to the AI and widens the `Privacy:` banner + cloud warning accordingly.
- Per-server subscription pricing; four tiers documented in `pricing-strategy.md`.

---

_Chronological build log (v0.1 → v1.2) follows._

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
- **ER diagram + reports** were eyeballed via headless-Edge screenshots during development; the ER auto-layout can still produce some crossing arrows on very dense schemas. (Automated test suite now exists — see Tests below.)

> Historical roadmap (Phases 1–3) is fully delivered through v1.2.0. The current forward plan is in **Next session — planned features** at the top of this section; Markdown/PDF/schema-diff/HTML-UX/triggers/computed-columns/retry/cache all shipped in the v1.1–1.2 line.

## Running

Packaged with `pyproject.toml` (setuptools); `pip install .` (or `-e .`) installs the **`sqldoc`** console command (entry point `sqldoc.cli:main`). You can still run it as a module from the repo root using the checked-in venv:

```bash
venv/Scripts/python.exe -m sqldoc.cli --server <host> --database <db> --username <user> --password <pw> --output docs.html
```

Key options (see `sqldoc/cli.py`):
- `--mode local|cloud` — `local` (default) calls Ollama at `http://localhost:11434`; `cloud` calls the Anthropic API.
- `--model` — model name. **Default is `None` and resolved per-mode** in `main()`: `llama3.1:8b` for local, `claude-haiku-4-5` for cloud. This split exists so the local Ollama tag never leaks into a cloud API call. Explicitly passing `--model` overrides for whichever backend is active; it is threaded all the way through to `_call_anthropic(prompt, model)` / `_call_ollama(prompt, model)`.
- `--no-ai` — skip all LLM calls, emit schema-only docs. Use this to iterate on extraction/rendering without a running LLM (also the fastest smoke test of the CLI plumbing).
- `--schemas` — comma-separated schema allowlist; filtering happens in `cli.py` *after* full extraction, and applies to tables, views, and procedures alike.
- `--concurrency` — parallel AI calls during enrichment (1-64, default 8). Threaded to all three `enrich_*` functions, which run their per-object calls through a `ThreadPoolExecutor`.
- `--connection-string` — full ODBC connection string as an alternative to `--server/--database/--username/--password`; takes precedence over them. The database name is parsed out (`DATABASE=`/`Initial Catalog=`) for labeling + snapshot/cache filenames. The extractor now takes a single connection string (`build_connection_string()` assembles it from parts).
- `--cache` / `--no-cache` — AI description cache (default `.sqldoc-cache/<database>.json`). Reuses descriptions for objects whose structural signature is unchanged; gitignored.
- `--config` — path to a `.sqldoc.yml` (default `.sqldoc.yml` in cwd if present). Any option can be set there; an explicit CLI flag overrides the config, which overrides the built-in default. Connection flags are optional when supplied via config.
- `--yes` / `-y` — bypass the cloud confirmation prompt (below) for non-interactive/CI runs.

**Privacy posture — local-first by design.** Local mode is the default; nothing leaves the network unless `--mode cloud` is explicitly chosen. The banner prints a `Privacy:` line stating the egress posture every run. `--mode cloud` additionally prints a warning and blocks on an interactive `click.confirm` (defaults to "no") before any network call — `--yes` skips only the prompt, not the warning. Note the data boundary: **only schema metadata** (table/column names, types, keys, row counts, existing `MS_Description` text) is ever sent to Anthropic — the extractor queries only `sys.*` catalog views and never reads table row data.

`--mode cloud` needs `ANTHROPIC_API_KEY`, loaded from `.env` via `python-dotenv` (`load_dotenv()` runs at import in `cli.py`).

## Dependencies

Pinned in `requirements.txt`: `click`, `pyodbc`, `anthropic`, `jinja2`, `requests`, `python-dotenv`, `PyYAML` (also installed in the checked-in `venv/`). Extraction requires the **ODBC Driver 18 for SQL Server** to be installed on the host (see the `DRIVER=` string in `extractor.py`) — this is a system package, not a pip dependency.

## Tests

Real pytest suite under `tests/` — **no live SQL Server or Ollama required** (pyodbc + the LLM calls are mocked). Run with `venv/Scripts/python.exe -m pytest -q` (or `pip install -e .[test]` for the `pytest`/`pypdf` extra). Coverage: extractor parsing via a fake pyodbc layer (`tests/conftest.py`), AI retry + cache, snapshot diffing, all three renderers, the PII engine, and CLI flag/command-group behavior. The old ad-hoc `test_*.py` scripts were removed in the v1.1 cleanup.

## Architecture

A linear pipeline, one module per stage, orchestrated by `cli.py`:

1. **`extractor.py`** — the only DB-facing code. Defines the shared-currency dataclasses: `Table`, `Column`, `Index` (attached to a `Table`), `View`, `Parameter`, `StoredProcedure`. `extract_metadata()` queries `sys.*` catalog views: tables + row counts, a per-table `sys.columns` join for PK/FK/`extended_properties` (`MS_Description`), and a per-table `sys.indexes`/`sys.index_columns` query (grouping key vs included columns). `extract_views()` and `extract_procedures()` add views (with `sys.sql_modules.definition`) and procedures (with `sys.parameters`). Any real `MS_Description` is captured here — the AI stage only fills descriptions left empty.

2. **`ai.py`** — `enrich_tables()`/`enrich_views()`/`enrich_procedures()` mutate the objects in place, setting `.description`. Two backends behind a `mode` switch: `_call_ollama` (HTTP to localhost) and `_call_anthropic` (SDK, shared lazily-created client, default model `claude-haiku-4-5`). Each `enrich_*` builds a flat list of independent per-object work units and runs them through a `ThreadPoolExecutor` (`--concurrency`, default 8) — each unit writes only its own object's `.description`, so there is no cross-thread shared state. View/proc prompts are **metadata-only** (names + columns/params, never the SQL definition) to keep the cloud data boundary unchanged.

3. **Renderers (one per format, same signature `(database, tables, output, views, procedures)`)** — `cli.py` picks one from `--format`/extension:
   - **`renderer.py`** `render_html()` — groups each object type by schema and renders the single `HTML_TEMPLATE` via an **autoescaping** Jinja `Environment` (all CSS inlined) to one standalone file. Autoescaping matters: view/proc definitions contain `<`/`>`/`&`.
   - **`markdown_renderer.py`** `render_markdown()` — single `.md` for GitHub wikis (TOC + anchor links, escaped table cells, fenced SQL).
   - **`pdf_renderer.py`** `render_pdf()` — `fpdf2` multi-page PDF; imported lazily so the other formats don't require it; Latin-1 text sanitization.

4. **`snapshot.py`** — orthogonal to rendering. `build_snapshot()` serializes the schema **structure** (not descriptions/rows) to JSON; `diff_snapshots()` + `iter_diff_lines()` produce the git-diff-style change report that `cli.py` prints (colored) and then re-saves the snapshot. Runs after schema-filtering, before enrichment, so it works with `--no-ai`.

5. **`pii.py` + `pii_renderer.py`** — the `sqldoc scan` path (independent of the doc pipeline; reuses `extract_metadata`). `pii.py` holds the `PII_CATEGORIES` catalog (name patterns → severity + regulations + action), a camelCase-aware matcher (`\b` patterns match tokenized names, others match the compact name), `scan_tables()` → `Finding`s with risk/confidence, and `confirm_with_sampling()` (opt-in `--sample`: reads ≤5 values per column, AI-confirms, **never stores samples**). `pii_renderer.render_pii_html()` writes the compliance report (dashboard, risk filter, CSV export via `tojson`). If you add a PII category, edit only `PII_CATEGORIES`.

The dataclasses flow through unchanged: extractor builds them → ai enriches them → renderers read them. If you add a **field**, touch the extractor, `ai.py` (if it needs a description), **all three renderers**, and `snapshot.py` (if it's a structural field worth diffing). If you add a new **object type**, also thread it through `cli.py` (extract → schema-filter → snapshot → enrich → render).
