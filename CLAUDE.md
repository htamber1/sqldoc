# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sqldoc` is a CLI that connects to a **SQL Server** database, extracts schema metadata, uses an LLM to generate plain-English descriptions of each table and column, and renders a single self-contained HTML documentation file. It has grown into a **seven-command** platform. The CLI is a command group: **`sqldoc doc`** (documentation), **`sqldoc scan`** (PII/compliance scan), **`sqldoc health`** (DMV performance/health), **`sqldoc quality`** (aggregate data profiling), **`sqldoc intel`** (schema intelligence), **`sqldoc insights`** (AI-powered: NL-to-SQL, anomalies, glossary, relationship inference), and **`sqldoc comply`** (HIPAA/GDPR/PCI-DSS reports, data lineage, access audit); a `DefaultGroup` routes `sqldoc <options>` (no subcommand) to `doc` for backward compatibility. Entry point is `sqldoc.cli:cli`.

## Project status (v1.4.1, as of 2026-07-11)

Shipped, **MIT-licensed, and live on PyPI** (`pip install sqldoc`) — a seven-command CLI: **`sqldoc doc`** (documentation), **`sqldoc scan`** (PII/compliance), **`sqldoc health`** (DMV health), **`sqldoc quality`** (data profiling), **`sqldoc intel`** (schema intelligence), **`sqldoc insights`** (AI insights), **`sqldoc comply`** (compliance expansion). Four release tags **v1.0.0 → v1.4.1** pushed to `github.com/htamber1/sqldoc` (latest **v1.4.1**, MIT license). **162 pytest tests** passing (mocked — no live SQL Server/Ollama). **CI is live** on GitHub (`.github/workflows/main.yml`). Validated end-to-end against a local `AdventureWorks2022` (71 tables / 20 views / 10 procs / 10 triggers / 10 computed columns; `sa`/`SqlDoc123!`). Version is single-sourced in `sqldoc/__init__.py` (`__version__`) — `cli.py` banners + `--version` and `sarif.py` read it; `pyproject.toml` is the only other place to bump.

### What's built (all shipped + tested)
**`sqldoc doc`** — `extractor.py` (tables, columns incl. PK/FK/**computed**, indexes, views+procs with definitions, **triggers**; single connection string via `build_connection_string()` or `--connection-string`) → `ai.py` (local Ollama / cloud Anthropic; `--concurrency`; retry+backoff; structural **description cache** `--cache`; metadata-only prompts) → renderers: **HTML** (`renderer.py` — dark theme, sidebar nav tree, interactive ER diagram, type filter+search, Copy SQL, color-coded row counts), **Markdown** (`markdown_renderer.py`), **PDF** (`pdf_renderer.py`/fpdf2); `--format`/extension dispatch. **Schema change detection** (`snapshot.py`, `--snapshot`).

**`sqldoc scan`** — `pii.py` (~21 PII categories → HIGH/MEDIUM/LOW + HIPAA/GDPR/PCI-DSS + action; camelCase-aware matcher; type confirmation; numeric confidence score + `--confidence-threshold`; per-column `pii_allowlist:`; optional AI `--sample` with values never stored; **custom categories** via `.sqldoc.yml` `pii_patterns:`) → `pii_renderer.py` (dark compliance HTML: dashboard, risk filter, CSV export). **PII drift** (`--baseline`), **SARIF export** (`sarif.py`, `--sarif`), **JSON** (`--json`), **CI gate** (`--fail-on high|new-high`).

**`sqldoc health`** — `health.py` (four DMV checks: slow queries `sys.dm_exec_query_stats`, dead tables `sys.dm_db_index_usage_stats`, missing indexes `sys.dm_db_missing_index_details` with generated `CREATE INDEX`, index fragmentation `sys.dm_db_index_physical_stats`; each check isolated so a missing `VIEW SERVER STATE` degrades that section only; reads statistics, never row data) → `health_renderer.py` (dark HTML dashboard + `build_health_json` for `--json`). Flags: `--top`, `--min-fragmentation`, `--min-pages`, `--schemas`.

**`sqldoc quality`** — `quality.py` (aggregate-only data profiling: per-column null rate, distinct/cardinality, min/max, blank-string count, most-frequent values `--top-values`; full-row duplicate detection via GROUP BY, `--no-duplicates` to skip; each column/table isolated in try/except; **reads row data in aggregate** — never row dumps, nothing leaves the machine) → `quality_renderer.py` (dark HTML with flag filters + `build_quality_json` for `--json`). Prints a local-only notice + confirm prompt (`--yes`).

**`sqldoc intel`** — `intel.py` (metadata-only schema intelligence: naming-convention analyzer via a dominant-style vote per identifier class + PK naming; orphaned-FK detector — `<Table>ID`-shaped columns without a constraint where the table exists; impact analysis — inbound FK graph + word-boundary search of view/proc/trigger definitions for "what breaks if you drop this table"; migration generator from a baseline snapshot diff via `generate_migration`, `--baseline` + `--migration-out`) → `intel_renderer.py` (dark HTML + `build_intel_json` for `--json`).

**`sqldoc insights`** — `insights.py` (AI + heuristic: NL-to-SQL via `--ask` — schema-grounded T-SQL, metadata only; heuristic **anomaly detection** — no-PK, generic names, missing audit columns, date/number/bool type mismatches, wide tables; AI **business glossary** — one term+definition per table, threaded, `--no-glossary`; heuristic **relationship inference** — likely FKs with confidence + `ALTER TABLE ADD CONSTRAINT` DDL. AI parts via `sqldoc.ai` with mode/`--no-ai`/cloud-confirm; heuristics always run) → `insights_renderer.py` (dark HTML w/ searchable glossary + `build_insights_json` for `--json`). NOTE: keep all CLI help/echo strings ASCII — `→` (U+2192) crashes cp1252 Windows consoles; em-dash `—` is cp1252-safe.

**`sqldoc comply`** — `comply.py` (compliance expansion, builds on `pii.scan_tables`, metadata only: **per-regulation sections** — findings grouped by HIPAA/GDPR/PCI-DSS with a `REGULATION_CONTROLS` catalog; **data lineage** — `build_lineage` matches table names in view/proc definitions, with `INSERT…SELECT` giving directional `procedure-write` flows; **access audit** — `extract_permissions` reads `sys.database_permissions` (object-level grants), `build_access_alerts` cross-refs grants against PII-bearing tables, DENY excluded, degrades without VIEW DEFINITION; honours `pii_patterns:`/`pii_allowlist:`; `--no-access-audit`) → `comply_renderer.py` (dark HTML + `build_comply_json` for `--json`).

**JSON export** — `json_renderer.py` (`sqldoc doc --format json` / `.json` extension, full model via `dataclasses.asdict`) and machine-readable findings for `scan --json` / `health --json` / `quality --json` / `intel --json`.

**Infra** — `pyproject.toml` + `sqldoc` console entry point (group via `DefaultGroup`; bare `sqldoc <opts>` → `doc`); pytest suite (**162 tests**) + `tests/conftest.py` fake-pyodbc (token-routed cursor: extractor + DMV/quality/permission queries; per-command `fake_*_rows` fixtures); GitHub Actions CI (`.github/workflows/main.yml` on the remote); `PUBLISHING.md`; `pricing-strategy.md`; `CHANGELOG.md`.

### Release / distribution state
1. **CI is live** — `.github/workflows/main.yml` runs on the remote. The old redundant `ci.yml` was deleted (2026-07-11) now that `main.yml` covers it.
2. **PyPI** — published from `~/.pypirc` (`__token__`); `pip install sqldoc` works. **1.4.0** (2026-07-11) shipped the seven-command platform; **1.4.1** (2026-07-11) added the **MIT License** (`LICENSE`, `license = {text = "MIT"}` + OSI classifier in `pyproject.toml`, README badge). README (the PyPI long description) covers the seven commands + a comparison vs Redgate SQL Doc / Dataedo. NOTE: MIT makes the project genuinely open-source, which **supersedes the license-key/entitlement paid-tier plan** in `pricing-strategy.md` — anyone can fork/remove gating; a viable model is now hosted/support/dual-license, not code gating. Reconcile `pricing-strategy.md` with the MIT decision.
3. **GitHub Releases** — Release pages for **v1.4.0 and v1.4.1 are done**. Still TODO: create Release pages for **`v1.2.0`** and **`v1.3.0`** from the CHANGELOG (paste-ready notes were provided in chat). The annotated tags already exist.

### Shipped in v1.3.0 / v1.4.0 / v1.4.1
- **v1.3.0** — JSON export (`doc --format json` + `--json` on the analysis commands); constraints (check/unique/default + FK actions); scan depth (6 new PII categories, `--confidence-threshold`, `pii_allowlist:`); `--include-definitions`; and the `health`, `quality`, `intel` commands.
- **v1.4.0** — **`sqldoc insights`** (NL-to-SQL via `--ask`, heuristic anomaly detection, AI business glossary, relationship inference) and **`sqldoc comply`** (per-regulation HIPAA/GDPR/PCI-DSS reports + controls, data-lineage tracking, access audit over `sys.database_permissions`).
- **v1.4.1** — MIT License (`LICENSE`, `pyproject` metadata, README badge). No code changes.

### Next session — multi-database adapter architecture (the big one)
**Goal:** support **PostgreSQL, MySQL, and Azure SQL** alongside SQL Server, with all seven commands working through a common adapter interface. This is the highest-leverage next feature (it 4×'s the addressable market and directly answers the Dataedo "20+ databases" gap in the README comparison).

Design:
- **`adapters/` package** with a **`base.py` `DatabaseAdapter` ABC** defining the metadata surface every command needs — the current `extractor.py` functions become the contract: `extract_metadata() -> list[Table]`, `extract_views()`, `extract_procedures()`, plus the analysis-specific queries (`health` DMVs, `quality` aggregates, `comply` permissions). Keep the shared dataclasses (`Table`/`Column`/…) as the dialect-neutral currency the whole pipeline already flows through — adapters populate them; renderers/analysis stay unchanged.
- **Concrete adapters**: `sqlserver.py` (refactor today's `extractor.py`/DMV/permission SQL into it — behavior-preserving), `postgres.py` (`information_schema` + `pg_catalog`; `psycopg`), `mysql.py` (`information_schema`; `mysql-connector-python` or `PyMySQL`), `azuresql.py` (subclass `sqlserver.py` — same T-SQL, different conn(ODBC/`azure-identity` auth); note Azure SQL DB lacks some server-scoped DMVs, so `health` must degrade gracefully like it already does for permissions).
- **Auto-detection from the connection string** + an explicit **`--dialect {sqlserver,postgres,mysql,azuresql}`** flag that overrides. Detect by driver/scheme (`postgresql://`, `mysql://`, `DRIVER={ODBC Driver 18 for SQL Server}`, `*.database.windows.net`). Add `dialect` to `CONFIG_KEYS`.
- **Per-dialect capability flags** — not every check exists everywhere (e.g. SQL Server DMV `sys.dm_db_missing_index_details` has no exact MySQL analogue). Each adapter advertises which `health`/`quality`/`comply` features it supports; unsupported ones render an explicit "not available on <dialect>" section (reuse the existing degrade-to-`errors` pattern).
- **PII/intel/insights are already dialect-neutral** (they run on the populated dataclasses), so most of the work is the extraction layer + dialect-specific SQL for `health`/`quality`/`comply`.

Testing: extend the fake-DB harness so `tests/conftest.py` can emulate each dialect's catalog rows; add per-adapter tests. Keep the token-routed fake-cursor approach. Aim to hold the "no live DB needed" property.

Rollout: land as a minor bump (**v1.5.0**) once SQL Server + at least Postgres pass; MySQL/Azure can follow. Update the README comparison ("Databases" row) and the privacy section (the `sys.*`-only claim becomes dialect-specific).

### Also pending (non-code)
- **Reconcile `pricing-strategy.md` with the MIT decision** — the four-tier license-key/entitlement plan no longer fits an MIT-licensed project (anyone can fork and strip gating). Rework toward hosted SaaS / paid support / dual-license / sponsorware, or explicitly commit to fully-free + a services model. This blocks any monetization messaging.
- **GitHub Release pages for `v1.2.0` and `v1.3.0`** (v1.4.0/v1.4.1 done) — paste-ready notes are in chat history.
- **Go-to-market planning** — with MIT + PyPI live, decide launch surface (Show HN / r/SQLServer / r/dataengineering / LinkedIn), a landing page or GitHub Pages demo (host a sample HTML report per command), and positioning vs Redgate SQL Doc (free + AI + compliance) and Dataedo (CLI + SQL-Server-deep + privacy-first). The multi-DB work above is a prerequisite for a broad "database documentation tool" pitch.

### Deferred / smaller
- **Deepen `insights`** — optional AI review pass over the model for subtler smells; enrich relationship inference with `quality` cardinality/overlap signals.
- **Deepen `comply`** — column-level lineage; resolve role membership + server-level rights in `extract_permissions`; per-regulation CSV/PDF export.
- **ER layout toggles** (key-columns-only / connected-only); **`--dry-run` cloud cost estimate**; `.env`-driven credentials.

### Standing decisions
- SQL definitions stay **out of AI calls by default** (metadata-only cloud boundary). The opt-in **`--include-definitions`** flag (shipped, post-1.2.0) sends view/proc/trigger bodies to the AI and widens the `Privacy:` banner + cloud warning accordingly.
- **Licensing: MIT** (as of v1.4.1). `pricing-strategy.md`'s per-server subscription / license-key tiers are now **superseded** and need reworking (see "Also pending") — code-gating an MIT project doesn't hold.
- **Dialect-neutral core**: the extractor dataclasses (`Table`/`Column`/…) are the currency the whole pipeline flows through; the planned multi-DB adapters populate them so renderers + analysis stay dialect-agnostic. Preserve this boundary when adding adapters.

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
