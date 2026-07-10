# Changelog

All notable changes to **sqldoc** are documented here. The format loosely
follows [Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-07-10

First stable release. `sqldoc` connects to a SQL Server database, extracts its
schema, optionally writes plain-English descriptions with an LLM, and renders a
polished, self-contained documentation set — as an interactive HTML app, a
GitHub-wiki Markdown file, or a PDF. It also tracks schema drift between runs.

### Object coverage
- **Tables** with row counts, and **columns** with data types, nullability,
  primary/foreign keys (with cross-references), **computed columns** (with their
  expression), and any existing `MS_Description` extended properties.
- **Indexes** (clustered/nonclustered, unique/PK), separating key vs. included
  columns.
- **Views** and **stored procedures**, each with their full SQL definition;
  procedures also list parameters and direction.
- **Triggers** (AFTER / INSTEAD OF, events, enabled state, definition).

### AI-generated descriptions
- Two backends behind a `--mode` switch: **local** (Ollama, default) and
  **cloud** (Anthropic); per-mode default models so a local tag never leaks into
  a cloud call.
- **Concurrent** enrichment via a thread pool (`--concurrency`) — ~5× faster
  than the original serial path.
- **Retry with exponential backoff + jitter** around every LLM call.
- **Description cache** (`.sqldoc-cache/<db>.json`, `--cache`/`--no-cache`) keyed
  by a structural signature, so re-runs only regenerate objects that changed —
  turning an incremental run from seconds-per-object into near-instant.

### Output formats
- **HTML** — a single self-contained dark-themed app (no external assets):
  collapsible **sidebar navigation tree**, an **interactive ER diagram**
  (FK-connected tables only, left-to-right schema bands, schema-colored arrows,
  hover-to-spotlight, click-to-jump), **type filter** (All/Tables/Views/
  Procedures) composed with real-time **search**, **Copy SQL** buttons on every
  definition, and **color-coded row counts** (green = has rows, gray = empty).
- **Markdown** — a single `.md` for GitHub wikis: schema-grouped table of
  contents with anchor links, column/index tables, and fenced SQL definitions.
- **PDF** — a multi-page report via `fpdf2` (pure-Python, no system libraries).
- Format is chosen by `--format` or inferred from the output extension.

### Schema change detection
- Each run writes a **structural JSON snapshot**; the next run diffs against it
  and prints a **git-diff-style report** — added/dropped tables, added/dropped
  columns, and type/nullability/key changes, plus view/proc add/remove.
  Snapshots capture structure only (never descriptions or row data).

### Connection & configuration
- Connect with discrete flags (`--server/--database/--username/--password`) or a
  single **`--connection-string`** (enterprise/Azure).
- **`.sqldoc.yml` config file** — any option can live in config; precedence is
  CLI flag > config > default.
- `--schemas` allowlist; `--yes` to bypass the cloud confirmation for CI.

### Distribution
- Packaged with **`pyproject.toml`** (setuptools) and a **`sqldoc` console entry
  point** — `pip install .` gives a first-class `sqldoc` command.
- **pytest suite** (40 tests) covering extraction (mocked pyodbc), AI retry +
  cache, snapshot diffing, all three renderers, and CLI flag combinations — no
  live SQL Server or Ollama required.

### Privacy & architecture decisions
- **Local-first by design.** Local mode is the default; nothing leaves the
  network unless `--mode cloud` is explicitly chosen, which prints a warning and
  blocks on a confirmation.
- **Row data is never read.** The extractor queries only `sys.*` catalog views —
  never a `SELECT` against user tables.
- **Tight cloud boundary.** Only schema metadata (names, types, keys, row
  counts, existing `MS_Description`) is ever sent to the API. View/procedure/
  trigger **SQL definitions are extracted and rendered locally but never sent to
  the model** (a future opt-in `--include-definitions` may relax this).
- **Autoescaping renderer.** The HTML is rendered through an autoescaping Jinja
  environment so SQL definitions containing `<`, `>`, `&` render as text.
- **Linear, testable pipeline.** `extractor → ai → renderer(s)`, orchestrated by
  `cli.py`, with `snapshot.py` orthogonal to rendering.

### Competitive advantages
- **vs. Redgate SQL Doc** — comparable object coverage plus an interactive,
  self-contained HTML app (live ER diagram, sidebar, search/filter) and
  AI-written descriptions, with a local-first privacy posture.
- **vs. Dataedo** — schema change detection (a premium Dataedo feature) is
  built-in, alongside multi-format export and an open, scriptable CLI.
- **AI descriptions** that read like a human wrote them, cached so they cost
  almost nothing to keep up to date.

## [0.1.0] — initial

- Initial pipeline: `sys.*` extraction of tables/columns/keys, a first pass at
  Ollama/Anthropic descriptions, and a single-file HTML renderer grouped by
  schema. Privacy guardrails, `README`, `requirements.txt`, and repo hygiene.
