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

_Second wave — DONE & validated (2026-07-10):_
- **Markdown export** (`markdown_renderer.py`) — single-file `.md` for GitHub wikis: stats, schema-grouped TOC with anchor links, column/index tables, view/proc SQL definitions in `<details>` + fenced ```sql. Pipes/newlines escaped in cells.
- **PDF export** (`pdf_renderer.py`, `fpdf2`) — multi-page A4 with title/stats, schema-grouped Tables/Views/Procedures, bordered tables, monospace definitions, footer page numbers. Pure-Python (no system libs); lazily imported; Latin-1 text sanitization. Validated to a 54-page PDF.
- **Schema change detection** (`snapshot.py`) — each run writes a structural JSON snapshot to `.sqldoc-snapshots/<database>.json` and diffs the next run against it, printing a git-diff-style report (added/dropped tables, added/dropped columns, type/nullability/pk changes, view/proc add/remove). `--snapshot PATH` / `--no-snapshot`. Snapshots capture structure only (no descriptions, no row data). Gitignored by default.
- **Format selection** — `--format html|markdown|pdf`, else inferred from the `--output` extension; all three renderers share the `(database, tables, output, views, procedures)` signature. cli dispatches; PDF import is lazy so html/markdown work without `fpdf2`.
- **HTML output UX (IDE-like)** — premium charcoal dark theme; sticky collapsible **sidebar navigation tree** (schema → tables/views/procs, click smooth-scrolls to the object's `obj-<schema>-<name>` card and flashes it); **interactive ER diagram** (FK-connected tables only, left-to-right schema bands, schema-colored arrows, hover-to-spotlight, click-to-jump); **type filter** (All/Tables/Views/Procedures) composed with real-time search; **Copy SQL** buttons on every definition; **color-coded row counts** (green = has rows, gray = empty). All CSS/JS inlined in `HTML_TEMPLATE`; card anchors use the shared `obj-` id scheme.

_Decisions:_
- **SQL definitions stay out of AI calls for now.** View/proc definitions are extracted + rendered locally but never sent to the model, holding the cloud data boundary at "names/types/keys/row counts." A future **`--include-definitions`** opt-in flag will let users trade the wider boundary for richer AI descriptions that read the definition body; it must update the `Privacy:` banner + cloud warning to state that definitions are being sent, and stay off by default.

_Third wave — DONE (2026-07-10):_ **triggers + computed columns** (extractor + all 3 renderers); **`ai.py` retry/backoff** (exp. backoff + jitter, `MAX_ATTEMPTS`) around both LLM backends; **description cache** (`--cache`/`--no-cache`, `.sqldoc-cache/<database>.json`) keyed by `(model, kind, structural signature)` so unchanged objects are reused instead of regenerated. Dark mode shipped earlier.

_Deferred:_ remaining object types (constraints), ER layout polish (fewer columns / key-columns-only / connected-tables-only toggle).

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
- `--connection-string` — full ODBC connection string as an alternative to `--server/--database/--username/--password`; takes precedence over them. The database name is parsed out (`DATABASE=`/`Initial Catalog=`) for labeling + snapshot/cache filenames. The extractor now takes a single connection string (`build_connection_string()` assembles it from parts).
- `--cache` / `--no-cache` — AI description cache (default `.sqldoc-cache/<database>.json`). Reuses descriptions for objects whose structural signature is unchanged; gitignored.
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

3. **Renderers (one per format, same signature `(database, tables, output, views, procedures)`)** — `cli.py` picks one from `--format`/extension:
   - **`renderer.py`** `render_html()` — groups each object type by schema and renders the single `HTML_TEMPLATE` via an **autoescaping** Jinja `Environment` (all CSS inlined) to one standalone file. Autoescaping matters: view/proc definitions contain `<`/`>`/`&`.
   - **`markdown_renderer.py`** `render_markdown()` — single `.md` for GitHub wikis (TOC + anchor links, escaped table cells, fenced SQL).
   - **`pdf_renderer.py`** `render_pdf()` — `fpdf2` multi-page PDF; imported lazily so the other formats don't require it; Latin-1 text sanitization.

4. **`snapshot.py`** — orthogonal to rendering. `build_snapshot()` serializes the schema **structure** (not descriptions/rows) to JSON; `diff_snapshots()` + `iter_diff_lines()` produce the git-diff-style change report that `cli.py` prints (colored) and then re-saves the snapshot. Runs after schema-filtering, before enrichment, so it works with `--no-ai`.

The dataclasses flow through unchanged: extractor builds them → ai enriches them → renderers read them. If you add a **field**, touch the extractor, `ai.py` (if it needs a description), **all three renderers**, and `snapshot.py` (if it's a structural field worth diffing). If you add a new **object type**, also thread it through `cli.py` (extract → schema-filter → snapshot → enrich → render).
