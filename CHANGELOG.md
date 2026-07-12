# Changelog

All notable changes to **sqldoc** are documented here. The format loosely
follows [Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

## [1.9.0] — 2026-07-11

**Ecosystem reach: VS Code, dbt, and a board-level cross-database report.**
Three integrations that meet teams where they already work.

### Added — VS Code extension
- New `vscode-extension/` (plain CommonJS, no build step). Right-click a `.sql`
  file or folder (or use the Command Palette) for a **sqldoc** submenu:
  **Document This Database**, **Scan for PII**, **Run Health Check**, **View
  Documentation**. Results open in a **webview panel** using sqldoc's existing
  dark-themed self-contained HTML (a local-only CSP is injected so the report's
  inline styles/scripts run without any network access).
- Connection resolves from settings → a workspace `.sqldoc.yml` → a prompt.
  Settings: `sqldoc.connectionString`, `sqldoc.dialect`, `sqldoc.sqldocPath`,
  `sqldoc.documentArgs` (default `--no-ai`).
- `build-vsix.py` packages a valid `.vsix` without npm/vsce; ships
  **`sqldoc-vscode.vsix`** (install with `code --install-extension`).

### Added — dbt integration (`sqldoc dbt`)
- Auto-detects a dbt project (`dbt_project.yml` in the current directory or an
  immediate subdirectory; `--project-dir` to override).
- Parses each model's description, column descriptions, and tests from the
  `schema.yml` files under the project's model paths.
- **Merges dbt metadata with the live database schema** from sqldoc, matching
  models to tables and classifying every column as *matched* /
  *db-only* (undocumented) / *dbt-only* (drift), with a documentation-coverage
  percentage. Runs dbt-only with `--no-db` or when no connection is configured.
- Dark self-contained HTML + `--json` + `--verify-offline`. Metadata only.

### Added — multi-database access report (`sqldoc comply --all-databases`)
- Reads the top-level **`databases:`** list in `.sqldoc.yml` (each entry a name
  plus a connection string or discrete parts + optional dialect) and renders
  **one board-level report**: a *principal × database* matrix showing every
  user/role and their read/write/admin access to regulated data across the whole
  estate, side by side. Sorted by reach then risk.
- Each database is audited independently — a failure on one is recorded, not
  fatal. Dark self-contained HTML matrix + `--json` (`report_type`
  `compliance-multi`). Config example added to `.sqldoc.example.yml`.

### Notes
- 332 tests passing (all mocked). No breaking changes — all additions.

## [1.8.0] — 2026-07-11

**Deeper compliance, health, CI, and air-gap support.** Four feature areas that
make the existing commands more useful in regulated and locked-down
environments, plus a drop-in GitHub Action.

### Added — enhanced access audit (`sqldoc comply`)
- **Unified per-principal access view.** The access audit now collapses every
  object-level grant into **one row per user/role**, bucketed into a
  read / write / admin **permission level** (GRANT WITH GRANT OPTION escalates to
  admin), with the count of objects each principal can touch, how many hold PII,
  and the worst risk + regulations across them.
- **Group/role membership expansion.** Database roles are resolved from
  `sys.database_role_members` (SQL Server) / `pg_auth_members` (PostgreSQL) and
  shown with a **collapsible dropdown of each role's members** in the HTML
  report. Both the per-principal view and the membership map are in `--json`
  (`principals[]`, `role_members[]`).

### Added — unused-objects detector (`sqldoc health`)
- **Unused stored procedures** — procedures/functions with no execution recorded
  since the stats last reset, via `sys.dm_exec_procedure_stats` (SQL Server) and
  `pg_stat_user_functions` (PostgreSQL).
- **Potential duplicate tables** — pairs with similar names *and* overlapping
  column structure (fuzzy name match + column-name Jaccard overlap).
  Metadata-only and dialect-neutral.
- **Redundant indexes** — index keys that duplicate, or are a leading prefix of,
  another index on the same table (PK/unique never flagged as the redundant one).
  Metadata-only and dialect-neutral.
- All three surface in the HTML report (new sections + stat cards) and `--json`.

### Added — GitHub Action
- A ready-to-use composite action at `.github/actions/sqldoc/` (referenceable as
  `uses: htamber1/sqldoc-action@v1` once mirrored, or `./.github/actions/sqldoc`
  locally). Inputs for **command** (doc/scan/health), **connection-string**,
  **dialect** (auto-installs the matching driver extra), **output-path**,
  **fail-on-high-pii** (maps to `--fail-on high`), plus `json-output`,
  `extra-args`, `sqldoc-version`, and `python-version`. Ships a README with
  recipes, a `publish-action.sh` mirror script, and an example workflow.

### Added — offline / air-gap verification
- **`--verify-offline`** on every HTML-emitting command scans the rendered report
  for any external resource reference (CDN scripts, web fonts, remote images,
  protocol-relative URLs) and warns if the report is not air-gap safe.
- New `sqldoc/offline.py` detector; the test suite now **enforces** that all
  seven report templates are fully self-contained.
- README gained a prominent **"Air-gap ready"** section documenting the
  zero-egress posture.

### Notes
- 315 tests passing (all mocked — no live database/Ollama). No breaking changes:
  the new outputs are additive to the existing HTML/JSON reports.

## [1.7.0] — 2026-07-11

**`sqldoc agent` — a persistent background monitoring daemon.** Turns sqldoc from
a run-it-yourself CLI into a living database monitoring system: it polls your
databases on an interval, keeps documentation always-current, tracks schema
changes / health / PII risk over time, serves a local dashboard, and alerts you
when things change.

### Added — `sqldoc agent`
- **`sqldoc agent start`** launches a background daemon (detached process; PID +
  log under `~/.sqldoc/`). `--foreground` runs it inline. **`stop`** shuts it down
  gracefully (stop-flag file, terminate fallback). **`status`** shows what's
  monitored, last run times, PII score, and health issues. **`logs [-n N] [-f]`**
  tails the agent log.
- **Configurable polling** (default every 30 min) per database. Each poll
  extracts the schema, diffs it against the last snapshot, and **re-generates AI
  documentation only for changed objects** by reusing the per-database
  description cache. Handles **multiple databases simultaneously** (one poller
  thread each) across any supported dialect.
- **Local web dashboard** on `http://127.0.0.1:8080` (stdlib http.server, no new
  dependency): an overview of every database (PII risk score, health issues,
  table counts, last run), a per-database page with the schema-change timeline
  and health/PII trend sparklines, the always-current documentation at
  `/db/<name>/doc`, and a JSON API.
- **Notifications** via Slack incoming-webhooks and email (SMTP) for schema
  changes, new PII findings, and health degradation — with an `on` allowlist.
  Each channel is isolated so a failing webhook never interrupts monitoring.
- **State** lives in a local SQLite database (`~/.sqldoc/agent.db`): snapshots,
  per-database AI caches, rendered docs, run history, a change/alert timeline,
  and a metrics time-series. Configuration goes in the `.sqldoc.yml` `agent:`
  section (see `.sqldoc.example.yml`).
- Built as a proper threading service (dashboard thread + per-database poller
  threads, `stop_event`-driven shutdown). 47 new tests, including a real
  end-to-end run against a temporary SQLite database.

## [1.6.1] — 2026-07-11

Oracle support and cross-dialect compliance. A seventh engine — **Oracle
Database** — and the `comply` access audit + data lineage now run on PostgreSQL
and MySQL, not just SQL Server.

### Added — Oracle Database
- **`OracleAdapter`** (optional dependency: `pip install sqldoc[oracle]`) using
  the `oracledb` driver and the `ALL_*` data-dictionary views (`ALL_TABLES`,
  `ALL_TAB_COLUMNS`, `ALL_CONSTRAINTS`/`ALL_CONS_COLUMNS`, `ALL_INDEXES`,
  `ALL_TRIGGERS`, `ALL_VIEWS`, `ALL_PROCEDURES`, `ALL_ARGUMENTS`), scoped to one
  schema (owner). Auto-detected from `oracle://` URLs and `*.oraclecloud.com`.
  **Mock-tested only** — not yet run against a live Oracle instance (needs a
  licensed database).

### Added — `comply` on PostgreSQL & MySQL
- **Access audit** now runs on PostgreSQL and MySQL via the standard
  `information_schema.table_privileges` (SQL Server keeps `sys.database_permissions`).
  Grants are cross-referenced against PII findings exactly as before.
- **Data lineage** works across dialects: the `INSERT … INTO` detector now
  tolerates PostgreSQL/ANSI `"…"` and MySQL `` `…` `` identifier quoting, not
  just SQL Server `[…]`.
- **Live-validated**: PostgreSQL/Pagila (205 grants read, 29 access alerts on PII
  tables, 57 lineage flows) and MySQL/Sakila (table grants + 48 lineage flows).
- `PostgresAdapter` and `MySQLAdapter` now advertise `access_audit=True`.

### Changed
- `extract_permissions` and `collect_compliance` take the resolved adapter
  (dispatching the grant SQL by dialect) instead of a raw connection string.

## [1.6.0] — 2026-07-11

Two more databases and cross-dialect analysis. sqldoc now targets **six
engines** — SQL Server, Azure SQL, PostgreSQL, MySQL, **SQLite**, and
**Snowflake** — and the `health` and `quality` commands work on PostgreSQL and
MySQL, not just SQL Server.

### Added — SQLite
- **`SqliteAdapter`** using the Python-stdlib `sqlite3` driver (no extra
  dependency) — PRAGMA-based extraction (`table_info` / `foreign_key_list` /
  `index_list` / `index_info`) plus `sqlite_master` for views and triggers.
  Auto-detected from `*.db` / `*.sqlite` paths and `sqlite://` URLs.
- **Live-validated** against the **Chinook** sample database (11 tables, FKs,
  indexes) — `doc`, `scan`, `intel`, and `quality` all produce correct reports.

### Added — Snowflake
- **`SnowflakeAdapter`** (optional dependency: `pip install sqldoc[snowflake]`)
  using `INFORMATION_SCHEMA` for tables/columns/views/procedures and
  `SHOW PRIMARY KEYS` / `SHOW IMPORTED KEYS` for constraints. Auto-detected from
  `snowflake://` URLs and `*.snowflakecomputing.com`. **Mock-tested only** — not
  yet run against a live account.

### Added — `health` and `quality` on PostgreSQL & MySQL
- **`quality`** now runs on SQL Server, PostgreSQL, MySQL, and SQLite via a
  per-dialect `QualityProfile` (identifier quoting, `TOP` vs `LIMIT`, and
  type classification), with min/max stringified in Python to stay
  dialect-neutral.
- **`health`** now runs on PostgreSQL (dead tables via `pg_stat_user_tables`,
  slow queries via `pg_stat_statements`) and MySQL (dead tables via
  `performance_schema.table_io_waits_summary_by_table`, slow queries via
  `performance_schema.events_statements_summary_by_digest`). Checks with no
  analogue on a dialect (missing-index / fragmentation advice) degrade to an
  explicit note. Capabilities are advertised per adapter, so unsupported
  combinations (e.g. `health` on SQLite) are refused with a clear message.
- **Live-validated**: PostgreSQL/Pagila and MySQL/Sakila — `quality` profiles
  every column with zero errors, and `health` correctly flags a genuinely dead
  table and surfaces slow-query digests.

### Fixed
- **PostgreSQL transaction-abort cascade** — analysis connections now use
  autocommit, so one failed statement (a missing `pg_stat_statements` extension,
  or `MIN` on an unsupported type) no longer aborts the whole transaction and
  poisons every following query.
- `boolean` is no longer classified as order-comparable (PostgreSQL has no
  `MIN(boolean)`).

### Changed
- The adapter interface gained a `cursor(conn)` method so each dialect hands the
  analysis code a row type it can read uniformly (e.g. MySQL's dict cursor).
- `collect_health` / `collect_quality` now take the resolved adapter rather than
  a raw connection string.

## [1.5.1] — 2026-07-11

Live-validation release: the PostgreSQL and MySQL adapters were run end-to-end
against real databases in Docker (**Pagila** on PostgreSQL 16, **Sakila** on
MySQL 8) — `doc`, `scan`, and `intel` all produce correct reports. Two bugs
surfaced by real data are fixed.

### Fixed
- **PostgreSQL — partitioned tables.** A declaratively-partitioned table (e.g.
  Pagila's `payment`) was documented as its physical partitions
  (`payment_p2022_01` …) instead of one logical table. The table query now
  includes partition parents (`relkind = 'p'`) and excludes partition children
  (`relispartition`), and clamps the parent's `-1` row estimate to `0`. Pagila
  now documents as 15 tables (was 21), with `payment` present as one table.
- **MySQL — cursor compatibility.** `mysql-connector-python` 9.x removed the
  `named_tuple` cursor, which raised `unexpected keyword argument 'named_tuple'`
  on connect. The adapter now uses a `dictionary=True` cursor (supported across
  the C-extension and pure-Python connections and all recent versions).

### Validated
- **PostgreSQL / Pagila** — 15 tables, 7 views, 9 functions; PK/FK (with
  `RESTRICT`/`CASCADE` actions), grouped indexes, `last_updated` triggers, and
  accurate row counts all extracted correctly.
- **MySQL / Sakila** — 16 tables, 7 views, 6 procedures; PK/FK, ENUM/SET/YEAR
  column types, INSERT/UPDATE/DELETE triggers, composite UNIQUE constraints, and
  procedure output parameters all correct.

## [1.5.0] — 2026-07-11

Multi-database support. sqldoc is no longer SQL-Server-only: a new adapter
layer lets **`doc`, `scan`, `intel`, and `insights`** run against **PostgreSQL**
and **MySQL** (and **Azure SQL**, which reuses the SQL Server path), documenting
tables, columns, keys, indexes, triggers, constraints, views, and
functions/procedures through each engine's catalog. The dialect-neutral
extraction dataclasses are unchanged, so every renderer and analysis stays the
same regardless of source database.

### Added
- **`adapters/` package + `DatabaseAdapter` ABC** — the shared dataclasses moved
  here as the dialect-neutral "currency", plus a `Capabilities` advertisement of
  which commands each dialect can serve.
- **`PostgresAdapter`** (`information_schema` + `pg_catalog` via **`psycopg2`**,
  an optional dependency: `pip install sqldoc[postgres]`) — tables/columns with
  PK/FK/generated columns, structured indexes (key vs INCLUDE), triggers (with
  bitmask event decoding), CHECK/UNIQUE constraints, views, and
  functions/procedures with parameters.
- **`MySQLAdapter`** (`information_schema` via **`mysql-connector-python`**, an
  optional dependency: `pip install sqldoc[mysql]`) — the same object surface,
  `DATABASE()`-scoped; CHECK constraints on MySQL 8.0.16+.
- **`--dialect {sqlserver,azuresql,postgres,mysql}`** on every command, plus a
  `dialect` config key. Auto-detected from the connection string
  (`postgresql://`, `mysql://`, `*.database.windows.net`) when not given.
- **Optional-dependency extras** — `sqldoc[postgres]`, `sqldoc[mysql]`,
  `sqldoc[all]`. SQL Server users install nothing extra; a missing driver raises
  a clear, actionable error naming the package to install.

### Changed
- **`extractor.py` is now a thin back-compat shim** over `adapters.sqlserver`
  (the SQL Server T-SQL moved there verbatim). All existing
  `from sqldoc.extractor import ...` imports keep working unchanged.
- **`doc`/`scan`/`intel`/`insights` extraction routes through the resolved
  adapter**, so `--dialect` genuinely drives the right catalog queries.

### Notes / limitations
- **`health`, `quality`, and the `comply` access audit** remain **SQL Server /
  Azure SQL only** this release (they use DMV / aggregate / `sys.database_permissions`
  SQL that has no ported equivalent yet); they refuse other dialects with a clear
  message. `comply` regulation + lineage reporting works on all dialects.
- 209 tests pass (mocked — no live database required for any dialect).

## [1.4.1] — 2026-07-11

### Added
- **MIT License** — `sqldoc` is now formally open source under the MIT License
  (© 2026 Harsh Tamboli). Added a `LICENSE` file, wired `license = {text = "MIT"}`
  and the OSI MIT classifier into `pyproject.toml`, and added a license badge to
  the README. No code changes — a licensing/metadata release.

## [1.4.0] — 2026-07-11

Two AI/compliance capability areas land as new commands, taking sqldoc to seven
commands. **`sqldoc insights`** brings AI-powered analysis (natural-language-to-
SQL, schema anomaly detection, an auto-generated business glossary, and
relationship inference), and **`sqldoc comply`** expands compliance with
per-regulation HIPAA/GDPR/PCI-DSS reports, data-lineage tracking, and access
auditing. Both follow the established pattern: a dark HTML report plus
machine-readable `--json`.

### Added — `sqldoc comply` (compliance expansion)
A seventh command building on the PII scanner (schema + catalog metadata only —
no row data); dark HTML report + `--json`:
- **Per-regulation reports** — findings grouped by **HIPAA / GDPR / PCI-DSS**,
  each showing the exact regulated columns and the controls that regulation
  typically requires (an in-scope / no-findings verdict per regime).
- **Data lineage** — traces flows through view/procedure SQL: a view reads its
  source tables; a procedure's `INSERT … SELECT` is a directional
  table-to-table write.
- **Access audit** — object-level grants from `sys.database_permissions`
  cross-referenced with the PII findings ("which principals can read regulated
  columns"); DENY grants excluded, degrades gracefully without VIEW DEFINITION
  (`--no-access-audit` to skip). Honours `pii_patterns:` / `pii_allowlist:`.

### Added — `sqldoc insights` (AI-powered schema insights)
A sixth command combining heuristic and AI analysis (metadata only — never row
data); dark HTML report + `--json`:
- **NL-to-SQL** — `--ask "plain English question"` (repeatable) returns a
  schema-grounded T-SQL query.
- **Anomaly detection** (heuristic, always on) — tables with no primary key,
  generic column names, missing audit columns, and name/type mismatches (a
  `*Date` stored as `varchar`, a `*Amount` as text, an `Is*`/`*Flag` not `bit`),
  plus very wide tables.
- **Business glossary** — one AI-inferred business term + definition per table,
  rendered as a searchable glossary (`--no-glossary` to skip).
- **Relationship inference** — likely foreign keys between tables with no
  constraint, from column-name + PK-type matching, with a confidence score and
  a ready-to-run `ALTER TABLE … ADD CONSTRAINT`.
`--no-ai` still runs the heuristic anomaly + relationship analysis; cloud mode
warns + confirms (only schema metadata and your questions are sent).

## [1.3.0] — 2026-07-11

sqldoc grows from a two-command tool into a five-command database platform.
Three new analysis commands — **`sqldoc health`** (DMV performance/health),
**`sqldoc quality`** (aggregate data profiling), and **`sqldoc intel`** (schema
intelligence) — join **`doc`** and **`scan`**, each with a dark-themed HTML
report and machine-readable `--json`. Alongside them: JSON export for
documentation, full constraint extraction, a deeper PII scanner, and an opt-in
to feed SQL definitions to the AI.

### Added — `sqldoc intel` (schema intelligence)
A fifth command that analyzes the extracted schema (no row data):
- **Naming conventions** — infers the dominant identifier style (Pascal / snake
  / camel / UPPER) for tables and columns and flags outliers, plus a
  primary-key naming check.
- **Orphaned FKs** — columns named like a foreign key (`CustomerID`) that a
  table exists for, but which carry no FK constraint (implied, unenforced).
- **Impact analysis** — for each table, what depends on it (FKs pointing at it +
  views/procedures/triggers whose SQL references it): "what breaks if you drop
  this".
- **Migration generation** — with `--baseline <snapshot.json>`, a review-ready
  DDL script from the schema diff (`--migration-out` to save the `.sql`).
Dark HTML report + `--json`.

### Added — `sqldoc quality` (data-quality profiling)
A fourth command that profiles the data itself, in **aggregate only** (COUNT /
COUNT DISTINCT / MIN / MAX / GROUP BY — nothing leaves the machine, no AI):
- **Null-rate analysis** — per-column null count/rate, with a `high-null` flag
  at ≥50%.
- **Distribution** — distinct count/cardinality, min/max, blank-string count,
  and each column's most-frequent values (`--top-values`, truncated).
- **Duplicate detection** — full-row duplicates via GROUP BY over every
  groupable column, reported as duplicate groups + redundant rows
  (`--no-duplicates` to skip the heaviest check).
Dark HTML report with flag filters, plus `--json`. Reads row data, so it prints
a local-only notice and confirms before running (`--yes` / `-y` to skip).

### Added — `sqldoc health` (database health analysis)
A third command that reads SQL Server DMVs (server/DB statistics only — never
table row data) and writes a dark-themed HTML report (`--json` for a
machine-readable copy):
- **Slow queries** — costliest cached statements by average elapsed time
  (`sys.dm_exec_query_stats` + `sys.dm_exec_sql_text`).
- **Dead tables** — tables with rows and writes but no reads since the stats
  last reset (`sys.dm_db_index_usage_stats`).
- **Missing indexes** — optimizer suggestions ranked by benefit, each with a
  ready-to-review `CREATE INDEX` (`sys.dm_db_missing_index_details` + stats).
- **Index fragmentation** — indexes past `--min-fragmentation` (and
  `--min-pages`) with a REBUILD/REORGANIZE call
  (`sys.dm_db_index_physical_stats`).
Each check is isolated: a missing `VIEW SERVER STATE` permission degrades that
one section (noted in the report) instead of aborting. `--top` bounds the
query/index rankings; `--schemas` filters the table-scoped checks.

### Added
- **JSON export** — machine-readable output for programmatic consumers.
  `sqldoc doc --format json` (or an `.json` output extension) emits the full
  extracted model — tables, columns, indexes, triggers, views, procedures, and
  AI descriptions — as a single JSON document. `sqldoc scan --json PATH` writes
  the compliance summary plus every finding as JSON (mirrors `--sarif`).
- **Constraints** — the extractor now captures **CHECK** and **UNIQUE**
  constraints (per table), column **DEFAULT** expressions, and **FK referential
  actions** (`ON DELETE` / `ON UPDATE`: CASCADE / SET NULL / SET DEFAULT). These
  render in all four formats (HTML gets a per-table *Constraints* section plus
  default/action detail on columns; Markdown/PDF get equivalents; JSON includes
  them automatically) and participate in schema change detection (`--snapshot`
  reports added/removed checks & uniques and changed defaults/FK actions).
- **Scan depth** — six new PII categories (**Biometric**, **Criminal Record**,
  **Insurance / Policy**, **Vehicle / Registration**, **Device Identifier**,
  **Age**). Each finding now carries a numeric **confidence score**;
  `sqldoc scan --confidence-threshold 0.0-1.0` drops weak (name-only /
  type-mismatch) matches. A **per-column allowlist** (`.sqldoc.yml`
  `pii_allowlist:`) suppresses known-safe columns — entries match
  `schema.table.column`, `table.column`, bare `column`, or a glob
  (`dbo.*.Password`) — before sampling, reporting, gating, or the baseline.
- **`--include-definitions`** (opt-in) — sends the SQL bodies of views, stored
  procedures, and triggers to the AI for richer descriptions. Off by default;
  when on, the `Privacy:` banner and cloud-mode warning explicitly state the
  widened data boundary, and the description cache keys on the body so an edited
  definition regenerates. Without it, only schema metadata reaches the AI (the
  long-standing cloud boundary).

## [1.2.0] — 2026-07-10

Compliance scanner hardening for enterprise/CI workflows.

### Added
- **PII drift detection** (`sqldoc scan --baseline`) — snapshots findings and
  diffs the next scan, reporting new / resolved / risk-changed findings (like
  schema change detection, for regulated data).
- **SARIF 2.1.0 export** (`sqldoc scan --sarif`) — import PII findings into
  **GitHub Advanced Security** / **Azure DevOps** security dashboards.
- **CI gating** (`sqldoc scan --fail-on {high,new-high}`) — exit non-zero to
  fail a build on HIGH findings, or only on a *new* HIGH finding vs the
  baseline. A reference GitHub Actions workflow lives at
  `.github/workflows/ci.yml`.
- **Custom PII patterns** — define org-specific categories in `.sqldoc.yml`
  under `pii_patterns:` (checked before the built-in catalog).

### Changed / infrastructure
- GitHub Actions CI runs pytest (3.10–3.12) on push/PR; README CI badge.
- Removed the ad-hoc root `test_*.py` scripts (superseded by the pytest suite,
  now 79 tests). Added `PUBLISHING.md` (PyPI release walkthrough); package
  builds + `twine check` pass and the `sqldoc` name is free on PyPI.

## [1.1.0] — 2026-07-10

### Added — PII / compliance scanner (`sqldoc scan`)
sqldoc becomes a compliance tool as well as a documentation tool. A new
`sqldoc scan` command identifies columns that likely hold personal or regulated
data and produces a compliance report.

- **Detection** — a catalog of ~15 PII categories (SSN/National ID, payment
  card, passport/license, bank account, health, credentials, date of birth,
  email, phone, postal address, GDPR special category, financial, geolocation,
  name, online identifier). Matching combines a camelCase-aware **name analysis**
  with **data-type confirmation** (a string type confirms an email/name match; a
  contradicting type lowers confidence and risk).
- **Risk & regulation mapping** — each finding gets a **HIGH / MEDIUM / LOW**
  rating and maps to the regulation(s) it implicates (**HIPAA / GDPR / PCI-DSS**),
  with a recommended remediation action.
- **Optional AI data sampling** (`--sample`) — reads up to 5 values per flagged
  column and asks the LLM whether they look like real PII, adjusting confidence.
  **Sampled values are never stored** — only the verdict is kept. Sampling is
  opt-in and gated by a warning + confirmation (extra warning in cloud mode).
- **Compliance report** — a self-contained dark-themed HTML report: a risk
  summary dashboard, a regulation breakdown, a filterable findings table
  (by risk), recommended actions, and a client-side **Export CSV** button.

### Changed
- The CLI is now a command group: **`sqldoc doc`** (documentation, the previous
  behavior) and **`sqldoc scan`** (PII scan). For backward compatibility,
  `sqldoc --server ...` (no subcommand) still runs `doc`.

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
