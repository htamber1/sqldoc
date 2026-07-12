# sqldoc

[![CI](https://github.com/htamber1/sqldoc/actions/workflows/main.yml/badge.svg)](https://github.com/htamber1/sqldoc/actions/workflows/main.yml)
[![PyPI](https://img.shields.io/pypi/v/sqldoc.svg)](https://pypi.org/project/sqldoc/)
[![Python](https://img.shields.io/pypi/pyversions/sqldoc.svg)](https://pypi.org/project/sqldoc/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**A database documentation & compliance platform** for **SQL Server, PostgreSQL,
MySQL, SQLite, Snowflake, Oracle, and Azure SQL** — documentation, PII/compliance
scanning, health analysis, data-quality profiling, schema intelligence, AI
insights, compliance reporting, and a **background monitoring agent** with a live
dashboard and Slack/email alerts. One CLI, self-contained HTML reports, and
machine-readable `--json` for every command.

`sqldoc` connects to your database, reads its schema (and, for a couple of
commands, aggregate statistics — never row data), optionally uses an LLM to write
plain-English descriptions, and produces reports you can open in any browser or feed
to another tool. Documentation, PII scanning, schema intelligence, and AI insights
run on **all seven engines** via a common adapter layer; `health` and `quality` run on
SQL Server, PostgreSQL, and MySQL. Pick the engine with `--dialect` or let it
auto-detect from the connection string. SQLite uses the Python stdlib and SQL Server
ships in the core install; the Postgres/MySQL/Snowflake drivers are optional
installs (`pip install sqldoc[postgres]` / `sqldoc[mysql]` / `sqldoc[snowflake]` /
`sqldoc[all]`).

## Quick start

```bash
pip install sqldoc

# Document a database (schema-only, nothing leaves your machine):
sqldoc doc --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --no-ai --output docs.html

# Scan for PII / regulated columns:
sqldoc scan --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --output pii-report.html

# Or run it as a live monitoring agent (config in .sqldoc.yml, dashboard on :8080):
sqldoc agent start          # background daemon: polls, diffs, docs, alerts
sqldoc agent status         # what's monitored + last run times
sqldoc agent logs -f        # follow the log;  sqldoc agent stop  to shut down
```

Extraction needs the **Microsoft ODBC Driver 18 for SQL Server** on the host
([download](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server));
it is a system package, not a pip dependency. AI descriptions are **opt-in** and, by
default, run **locally** against [Ollama](https://ollama.com) — see the privacy
section below.

## The seven commands

| Command | What it does | Reads |
| --- | --- | --- |
| **`sqldoc doc`** | Schema documentation with AI descriptions, ER diagram, search. HTML / Markdown / PDF / JSON. | schema metadata |
| **`sqldoc scan`** | PII / compliance scan — ~21 categories mapped to HIPAA / GDPR / PCI-DSS, risk dashboard, SARIF, CI gate. | schema metadata |
| **`sqldoc health`** | DMV analysis — slow queries, dead tables, missing-index suggestions, index fragmentation. | server/DB statistics |
| **`sqldoc quality`** | Data-quality profiling — null rates, cardinality/distribution, duplicate detection. | data (aggregate only) |
| **`sqldoc intel`** | Schema intelligence — naming conventions, orphaned FKs, impact analysis, migration generation. | schema metadata |
| **`sqldoc insights`** | AI insights — natural-language-to-SQL, anomaly detection, business glossary, relationship inference. | schema metadata |
| **`sqldoc comply`** | Compliance reports — per-regulation HIPAA/GDPR/PCI-DSS scope + controls, data lineage, access audit. | schema + catalog metadata |

Every command writes a self-contained dark-themed HTML report and accepts `--json`
(or, for `doc`, `--format json`) for machine-readable output. `sqldoc` with options
but no subcommand runs `doc` for backward compatibility.

## 🔒 Privacy guarantee

**sqldoc runs on-premise by default and never reads your table row data for
documentation, scanning, or schema analysis.**

- **Local by default.** AI processing (in `doc` and `insights`) runs against an
  [Ollama](https://ollama.com) instance on your own machine unless you explicitly
  pass `--mode cloud`. Nothing leaves your network in local mode.
- **Row data is never read for metadata work.** `doc`, `scan` (without `--sample`),
  `intel`, `insights`, and `comply` query only `sys.*` catalog views and DMVs — no
  `SELECT` against your tables.
- **The commands that do touch data say so and stay local.** `quality` runs
  *aggregate-only* queries (COUNT / DISTINCT / MIN / MAX / GROUP BY) and prints a
  confirmation prompt; `scan --sample` reads ≤5 values per flagged column purely to
  score confidence and **never stores them**. Neither sends data off-network.
- **Cloud is opt-in and explicit.** `--mode cloud` prints a warning and requires
  confirmation before any network call, and sends only *schema metadata* (names,
  types, keys) — never row data. `--include-definitions` additionally sends
  view/procedure/trigger SQL bodies, with its own widened warning.

The worst-case disclosure in cloud mode is a column *name* like `Employee.Salary` —
never a salary.

## 🛡️ Air-gap ready

**Every HTML report is a single, fully self-contained file** — all CSS and
JavaScript are inlined, the ER diagram is inline SVG, and there are **no CDN
scripts, web fonts, or remote images**. Reports render identically on an
isolated, offline, air-gapped network — open the `.html` straight from disk with
no internet connection.

Verify it yourself on any generated report with the `--verify-offline` flag
(available on every command that emits HTML):

```bash
sqldoc scan --connection-string "..." --output pii.html --verify-offline
#   offline check: OK - fully self-contained, no external resources.
```

It scans the rendered HTML for any external resource reference (CDN `<script>`,
`<link>` stylesheet, web font `@import`, remote `<img>`, protocol-relative
`//host` URL) and warns if it finds one. Combined with `--mode local` (the
default) and the metadata-only boundary above, sqldoc runs end-to-end with **zero
network egress** — a good fit for regulated, classified, or on-prem-only
environments. The self-containment of all seven report templates is enforced by
the test suite.

## Installation

From PyPI:

```bash
pip install sqldoc              # SQL Server / Azure SQL (pyodbc) + SQLite (stdlib)
pip install sqldoc[postgres]    # + PostgreSQL (psycopg2)
pip install sqldoc[mysql]       # + MySQL (mysql-connector-python)
pip install sqldoc[snowflake]   # + Snowflake (snowflake-connector-python)
pip install sqldoc[oracle]      # + Oracle (oracledb)
pip install sqldoc[all]         # + all optional drivers
```

The dialect is auto-detected from the connection string (`postgresql://`,
`mysql://`, `snowflake://`, `oracle://`, `*.snowflakecomputing.com`,
`*.oraclecloud.com`, `*.db`/`*.sqlite`, `*.database.windows.net`) or set
explicitly with
`--dialect {sqlserver,azuresql,postgres,mysql,sqlite,snowflake,oracle}`. `doc`,
`scan`, `intel`, and `insights` run on all seven engines; `health` and `quality`
run on SQL Server, PostgreSQL, and MySQL; the `comply` access audit runs on SQL
Server, PostgreSQL, and MySQL. (Snowflake and Oracle are currently mock-tested,
not yet validated against a live instance.)

From source (editable/development install):

```bash
git clone https://github.com/htamber1/sqldoc.git
cd sqldoc
python -m venv venv
venv/Scripts/activate            # Windows;  source venv/bin/activate on macOS/Linux
pip install -e .[test]           # includes the pytest extras
```

**Requirements:** Python 3.10+, the ODBC Driver 18 for SQL Server, and — for AI
descriptions — either a running Ollama (local, default `llama3.1:8b`) or an Anthropic
API key (cloud). For cloud mode, put your key in a `.env` file:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Commands in depth

### `sqldoc doc` — documentation

Extracts tables, columns (incl. computed columns and constraints — PK/FK with
referential actions, CHECK / UNIQUE / DEFAULT), indexes, triggers, views and
procedures (with SQL definitions), enriches them with AI descriptions, and renders a
single self-contained report.

```bash
# Local AI (Ollama), HTML with ER diagram + search:
sqldoc doc --server localhost --database AdventureWorks2022 --username sa --password '***'

# Markdown / PDF / JSON — inferred from the output extension, or forced with --format:
sqldoc doc ... --output docs.md
sqldoc doc ... --output docs.pdf
sqldoc doc ... --format json --output schema.json
```

The HTML report is an IDE-like reading experience: a collapsible sidebar navigation
tree, an interactive ER diagram (schema-banded, FK arrows), real-time search + type
filter, per-table Constraints sections, one-click **Copy SQL** on every definition,
and color-coded row counts. `--include-definitions` sends view/proc/trigger bodies to
the AI for richer descriptions (opt-in; widens the data boundary).

**Schema change detection** — every run snapshots the schema *structure* and the next
run prints a git-style diff (new/dropped tables & columns, type/key/constraint
changes). `--snapshot` / `--no-snapshot`.

### `sqldoc scan` — PII / compliance

Flags columns likely to hold personal or regulated data by name + type, maps each to
**HIPAA / GDPR / PCI-DSS**, rates **HIGH / MEDIUM / LOW** with a numeric confidence
score, and writes a compliance dashboard (with CSV export).

```bash
sqldoc scan --server localhost --database AdventureWorks2022 --username sa --password '***'
```

- **`--confidence-threshold 0.0-1.0`** drops weak (name-only / type-mismatch) matches.
- **`--sample`** reads ≤5 values per column and asks the AI to confirm (values never
  stored; prompts first).
- **`pii_allowlist:`** in `.sqldoc.yml` suppresses known-safe columns
  (`schema.table.column`, bare `column`, or a glob like `dbo.*.Password`).
- **`pii_patterns:`** defines org-specific categories.
- **PII drift** (`--baseline`), **SARIF 2.1.0** (`--sarif`) for GitHub Advanced
  Security / Azure DevOps, **JSON** (`--json`), and a **CI gate**
  (`--fail-on high|new-high`).

### `sqldoc health` — DMV analysis

Reads SQL Server DMVs (statistics only) for the slowest cached queries, dead tables
(rows + writes but no reads), optimizer missing-index suggestions with a generated
`CREATE INDEX`, and fragmented indexes with a REBUILD/REORGANIZE call. Each check is
isolated: a missing `VIEW SERVER STATE` degrades that section, not the whole run.
`--top`, `--min-fragmentation`, `--min-pages`.

### `sqldoc quality` — data-quality profiling

Profiles the data in **aggregate only**: per-column null rate (with a high-null flag),
distinct count/cardinality, min/max, blank counts, most-frequent values
(`--top-values`), and full-row duplicate detection (`--no-duplicates` to skip). Prints
a local-only notice and confirms before running (`--yes` to skip).

### `sqldoc intel` — schema intelligence

Naming-convention analysis (dominant style + outliers, PK naming), orphaned-FK
detection (implied-but-unenforced relationships), impact analysis ("what breaks if you
drop this table"), and migration-script generation from a baseline snapshot
(`--baseline snapshot.json`, `--migration-out migration.sql`).

### `sqldoc insights` — AI insights

```bash
sqldoc insights --server localhost --database AdventureWorks2022 --username sa \
    --password '***' --ask "which customers placed the most orders last month?"
```

- **Natural-language-to-SQL** — `--ask "question"` (repeatable) returns a
  schema-grounded T-SQL query.
- **Anomaly detection** (heuristic, always on) — tables with no primary key, generic
  column names, missing audit columns, and name/type mismatches (a `*Date` stored as
  `varchar`, etc.).
- **Business glossary** — an AI-inferred term + definition per table, rendered as a
  searchable glossary (`--no-glossary` to skip).
- **Relationship inference** — likely foreign keys with a confidence score and a
  ready-to-run `ALTER TABLE … ADD CONSTRAINT`.

`--no-ai` still runs the heuristic anomaly + relationship analysis.

### `sqldoc comply` — compliance expansion

- **Per-regulation reports** — the scan findings grouped by HIPAA / GDPR / PCI-DSS,
  each showing the regulated columns and the controls that regime typically requires.
- **Data lineage** — flows through view/procedure SQL (a view reads its source tables;
  a proc's `INSERT … SELECT` is a directional write).
- **Access audit** — object-level grants from `sys.database_permissions`
  cross-referenced with the PII findings ("who can read regulated columns");
  `--no-access-audit` if the account lacks `VIEW DEFINITION`.

## Config file

Rather than repeating flags, drop a `.sqldoc.yml` in the working directory — every key
maps to the CLI option of the same name (an explicit flag wins over config, which wins
over defaults). Copy [`.sqldoc.example.yml`](.sqldoc.example.yml) to start.

```yaml
server: localhost
database: AdventureWorks2022
username: sa
mode: local
# password: better supplied via --password or .env than committed to disk
pii_allowlist:
  - dbo.Config.ContactEmail       # a support inbox, not personal PII
```

> `.sqldoc.yml` is **gitignored** because it can contain a password. Keep secrets out
> of it (use `--password` or `.env`) if you plan to share it.

## Live monitoring — `sqldoc agent`

Instead of running sqldoc by hand, run it as a persistent background agent that
keeps a living view of your databases:

```bash
sqldoc agent start          # spawn the background daemon (or --foreground)
sqldoc agent status         # what's monitored, last run, PII score, health
sqldoc agent logs -f        # tail the log
sqldoc agent stop           # graceful shutdown
```

Configure it under an `agent:` section in `.sqldoc.yml` (see
`.sqldoc.example.yml`) — one or more databases across any supported dialect, a
poll interval, a dashboard port, and optional Slack/email alerts. On each poll
the agent:

- extracts the schema and **diffs it** against the last snapshot;
- **re-generates AI documentation only for changed objects** (reusing the
  per-database description cache), so incremental runs are cheap;
- tracks **health** and a **PII risk score** over time;
- serves an always-current **dashboard** at `http://127.0.0.1:8080` — overview
  cards, a per-database schema-change timeline, trend sparklines, and the full
  generated docs;
- sends **notifications** (Slack webhook / email) on schema changes, new PII
  findings, and health degradation.

State lives in a local SQLite database (`~/.sqldoc/agent.db`); nothing about the
monitoring itself leaves your machine (AI descriptions follow the same
local-first boundary as the rest of sqldoc).

## How does it compare?

sqldoc overlaps with commercial documentation and data-catalog tools but takes a
free, CLI-first, multi-database, privacy-first angle. The table below is based on
publicly documented capabilities — verify current features against each vendor.

| Capability | **sqldoc** | Redgate SQL Doc | Dataedo |
| --- | --- | --- | --- |
| Price | Free / open source (MIT) | Paid (per-user) | Paid (per-user / repo) |
| Interface | CLI — scriptable, CI-friendly | Desktop GUI + CLI | Desktop app + web repo |
| Databases | SQL Server, PostgreSQL, MySQL, SQLite, Snowflake, Oracle, Azure SQL | SQL Server | 20+ (SQL Server, Oracle, PostgreSQL, MySQL, …) |
| Self-contained HTML docs | ✓ | ✓ (HTML / CHM / Word / Markdown) | ✓ (web catalog) |
| AI-written descriptions | ✓ (local Ollama **or** cloud) | ✗ | Partial (AI assist) |
| Runs fully offline / on-prem | ✓ (local mode, no data egress) | ✓ | ✓ (on-prem repo) |
| PII / sensitive-data detection | ✓ (+ HIPAA/GDPR/PCI mapping) | ✗ | ✓ (classification) |
| Compliance reports (HIPAA/GDPR/PCI-DSS) | ✓ | ✗ | Partial |
| DMV health (slow queries, missing indexes) | ✓ | ✗ | ✗ |
| Data-quality profiling (nulls, dupes) | ✓ | ✗ | ✓ |
| Business glossary | ✓ (auto-generated) | ✗ | ✓ (curated catalog) |
| Data lineage | ✓ (view/proc parsing) | ✗ | ✓ (advanced, cross-object) |
| Schema change detection / diff | ✓ | Partial | ✓ |
| Machine-readable JSON output | ✓ (every command) | Partial | ✓ (API) |
| Natural-language-to-SQL | ✓ | ✗ | ✗ |

**Where each shines.** Redgate SQL Doc is a polished static-documentation generator for
SQL Server (Word/CHM output, source-control integration). Dataedo is a mature,
multi-database data catalog with rich curated lineage and glossary and a team
repository. sqldoc is free and scriptable, focuses on SQL Server, adds AI descriptions
and a privacy-first local mode, and bundles PII/compliance, DMV health, and
data-quality analysis that the documentation tools don't cover.

## How it works

A linear pipeline, one module per stage, orchestrated by `cli.py`:

- `extractor.py` — the only DB-facing code for the metadata path; queries `sys.*`
  catalog views and builds the shared dataclasses (`Table` / `Column` / `Index` /
  `View` / `StoredProcedure` / constraints).
- `ai.py` — fills descriptions via Ollama (local) or the Anthropic SDK (cloud),
  concurrently, with retry + a structural description cache.
- Renderers — `renderer.py` (HTML), `markdown_renderer.py`, `pdf_renderer.py`,
  `json_renderer.py`, plus a dedicated `*_renderer.py` per analysis command.
- Analysis modules — `pii.py`, `health.py`, `quality.py`, `intel.py`, `insights.py`,
  `comply.py`, each with a `build_*_json` for machine-readable output.

Run the test suite (mocked — no live SQL Server or Ollama required) with
`pytest -q`.

## License

Released under the [MIT License](LICENSE) — © 2026 Harsh Tamboli. You are free to
use, modify, and redistribute sqldoc, including commercially, provided the license
and copyright notice are preserved.
