# sqldoc

[![CI](https://github.com/htamber1/sqldoc/actions/workflows/ci.yml/badge.svg)](https://github.com/htamber1/sqldoc/actions/workflows/ci.yml)

Automated documentation generator for **SQL Server** databases.

`sqldoc` connects to a SQL Server database, extracts its schema, uses an LLM to
write plain-English descriptions of every table and column, and renders it all
into a single self-contained HTML file you can open in any browser or hand to a
colleague.

## 🔒 Privacy guarantee

**sqldoc runs on-premise by default and never reads your data.**

- **Local by default.** In the default local mode, all AI processing runs against
  a [Ollama](https://ollama.com) instance on your own machine. **No data of any
  kind leaves your network** — not schema, not metadata, nothing.
- **Row data is never read.** sqldoc queries only SQL Server's `sys.*` catalog
  views. It does not issue a single `SELECT` against your tables, so actual row
  data is never read, stored, or transmitted — in *any* mode.
- **Cloud is opt-in and explicit.** Sending anything off-network requires
  `--mode cloud`, which prints a warning and requires interactive confirmation
  before making a network call. Even then, only *schema metadata* (table/column
  names, data types, keys, and row counts) is sent to the Anthropic API.

This makes sqldoc safe to run against production and regulated databases: the
worst-case disclosure in cloud mode is a column *name* like `Employee.Salary` —
never a salary.

## What it does

1. **Extracts** schema metadata from the `sys.*` catalog views — tables, columns,
   data types, primary/foreign keys, row counts, indexes, views (with their SQL
   definitions), stored procedures (with parameters), and any existing
   `MS_Description` extended properties.
2. **Enriches** it with AI-generated descriptions: a short summary for each table,
   view, and stored procedure, plus a one-line description for each column that
   doesn't already have one. Enrichment runs concurrently (see `--concurrency`),
   retries transient failures with exponential backoff, and caches descriptions
   so re-running only regenerates objects whose structure changed.
3. **Renders** a standalone HTML document — grouped by schema, with an ER diagram,
   real-time search, and collapsible view/procedure definitions, styled inline,
   no external assets or dependencies to serve.

### HTML output — an IDE-like reading experience

The HTML report is a self-contained, dark-themed app (one file, no external
assets) built for navigating large schemas:

- **Sidebar navigation tree** — a collapsible left panel lists every schema and
  its tables/views/procedures (type-tagged); click any item to smooth-scroll to
  its card. The whole sidebar and each schema node collapse.
- **Interactive ER diagram** — schema-banded left-to-right layout showing only
  FK-connected tables, with arrows colored by schema. Hover a table to spotlight
  its relationships; click it to jump to its documentation card.
- **Real-time search + type filter** — filter to All / Tables / Views /
  Procedures and search across names and columns at once.
- **Copy SQL** — one-click copy button on every view and stored-procedure
  definition.
- **Color-coded row counts** — green pills for populated tables, gray for empty
  ones, with thousands separators.

## Requirements

- **Python 3.10+**
- **Microsoft ODBC Driver 18 for SQL Server** installed on the host
  ([download](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)).
  This is a system package, not a pip dependency.
- For **local mode**: a running [Ollama](https://ollama.com) with a model pulled
  (default `llama3.1:8b` — `ollama pull llama3.1:8b`).
- For **cloud mode**: an Anthropic API key.

## Installation

```bash
python -m venv venv
venv/Scripts/activate        # Windows;  source venv/bin/activate on macOS/Linux
pip install .                # installs sqldoc and the `sqldoc` command
```

Use `pip install -e .` for an editable/development install. For cloud mode,
create a `.env` file in the working directory with your API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

sqldoc has two subcommands — **`sqldoc doc`** (generate documentation) and
**`sqldoc scan`** (scan for PII / compliance). For backward compatibility,
`sqldoc` with options but no subcommand runs `doc`:

```bash
sqldoc doc --server <host> --database <db> --username <user> --password <pw> \
    --output docs.html
# equivalently: sqldoc --server <host> ... --output docs.html
```

You can also run it as a module without installing (`python -m sqldoc.cli ...`).

```bash
python -m sqldoc.cli --server <host> --database <db> \
    --username <user> --password <pw> --output docs.html
```

### Examples

Local mode (default — uses Ollama, nothing leaves your network):

```bash
python -m sqldoc.cli --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --output docs.html
```

Cloud mode (Anthropic — prompts for confirmation before sending metadata):

```bash
python -m sqldoc.cli --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --mode cloud --output docs.html
```

Schema-only, no AI (fastest; nothing leaves the machine):

```bash
python -m sqldoc.cli --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --no-ai --output docs.html
```

Markdown export for a GitHub wiki (format inferred from the `.md` extension):

```bash
python -m sqldoc.cli --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --no-ai --output docs.md
```

PDF export (self-contained, no system libraries — uses `fpdf2`):

```bash
python -m sqldoc.cli --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --no-ai --output docs.pdf
```

### Options

| Option | Description |
| --- | --- |
| `--server` | SQL Server hostname or IP (**required**) |
| `--database` | Database name to document (**required**) |
| `--username` | SQL Server username (**required**) |
| `--password` | SQL Server password (**required**) |
| `--connection-string` | Full ODBC connection string — an alternative to the four flags above |
| `--output` | Output file path (default `documentation.html`) |
| `--format html\|markdown\|pdf` | Output format. Defaults to the `--output` extension (`.md`→markdown, `.pdf`→pdf), else HTML |
| `--mode local\|cloud` | AI backend: `local` (Ollama, default) or `cloud` (Anthropic) |
| `--model` | Model to use. Defaults per mode: `llama3.1:8b` (local), `claude-haiku-4-5` (cloud) |
| `--schemas` | Comma-separated list of schemas to include (default: all) |
| `--no-ai` | Skip AI descriptions, output schema only |
| `--concurrency` | Parallel AI calls during enrichment, 1-64 (default `8`) |
| `--snapshot` | JSON schema-snapshot path for change detection (default `.sqldoc-snapshots/<database>.json`) |
| `--no-snapshot` | Disable schema snapshot + change detection for this run |
| `--cache` | AI description cache path (default `.sqldoc-cache/<database>.json`) |
| `--no-cache` | Disable the AI description cache (always regenerate) |
| `--config` | Path to a config file (default `.sqldoc.yml` if present) |
| `--yes` / `-y` | Skip the cloud-mode confirmation prompt (for non-interactive/CI use) |

Instead of the four connection flags you can pass a single
`--connection-string` (handy for enterprise/Azure connection strings); the
database name is parsed from it for labeling. Any option (and the connection
flags) can also be supplied from a config file — see below.

```bash
python -m sqldoc.cli --connection-string \
  "DRIVER={ODBC Driver 18 for SQL Server};SERVER=host;DATABASE=Sales;UID=user;PWD=***;TrustServerCertificate=yes;" \
  --output docs.html
```

## Config file

Rather than passing the same flags every run, drop a `.sqldoc.yml` in the working
directory. Every key maps to the CLI option of the same name; an explicitly
passed CLI flag always overrides the config, which in turn overrides the built-in
defaults. Copy [`.sqldoc.example.yml`](.sqldoc.example.yml) to get started:

```yaml
server: localhost
database: AdventureWorks2022
username: sa
mode: local
concurrency: 8
# password: better supplied via --password than committed to disk
```

Then simply:

```bash
python -m sqldoc.cli --output docs.html          # reads .sqldoc.yml
python -m sqldoc.cli --mode cloud --output docs.html   # override just one setting
```

> `.sqldoc.yml` is **gitignored** because it can contain a database password.
> Keep secrets out of it (use `--password` or `.env`) if you plan to share it.

## Schema change detection

Every run writes a JSON snapshot of the schema's *structure* (object names,
column types, keys, indexes, parameters — never descriptions or row data) to
`.sqldoc-snapshots/<database>.json`. On the next run, sqldoc diffs the live
schema against that snapshot and prints what changed, like a git diff for your
database:

```text
Schema changes since last run  (.sqldoc-snapshots/AdventureWorks2022.json):
+ table    Sales.Promotion  (6 columns)
- table    dbo.LegacyAudit
~ table    HumanResources.Employee
    + column   PreferredName
    - column   MiddleName
    ~ column   MaritalStatus: type int -> nchar
+ view     Sales.vActiveCustomers
Schema changes: 1 table(s) added, 1 table(s) removed, 1 table(s) modified, 1 view/proc change(s)
```

New/dropped tables, new/dropped columns, and type/nullability/key changes are
all reported. The first run just saves a baseline. Disable with `--no-snapshot`,
or point somewhere specific with `--snapshot path.json`. Snapshots are
gitignored by default; commit them intentionally if you want cross-commit or CI
change tracking.

## PII / compliance scanning

`sqldoc scan` turns sqldoc into a compliance tool. It identifies columns that
likely hold personal or regulated data and writes a self-contained HTML
compliance report — a risk dashboard, per-column **HIGH / MEDIUM / LOW** ratings,
the regulation each finding maps to (**HIPAA / GDPR / PCI-DSS**), recommended
actions, and a CSV export.

```bash
# Name + data-type analysis only — reads no row data:
sqldoc scan --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --output pii-report.html
```

Detection matches column names (SSN, national ID, credit card, email, phone,
date of birth, passport, address, credentials, …) and confirms with the data
type. Add **`--sample`** to read up to 5 values per flagged column and have the
AI confirm whether they look like real PII:

```bash
sqldoc scan --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --sample --mode local --output pii-report.html
```

> `--sample` reads real values (which may be actual PII) purely to score
> confidence — **sampled values are never stored**, only the verdict. It is
> opt-in and prompts for confirmation; in cloud mode the samples are sent to the
> API, so prefer `--mode local` for sampling.

**PII drift** — each scan snapshots its findings; the next scan reports new,
resolved, and risk-changed findings (like schema change detection, for regulated
data). `--baseline PATH` / `--no-baseline`.

**SARIF export** — add `--sarif findings.sarif` to also emit SARIF 2.1.0 for
**GitHub Advanced Security** / **Azure DevOps**, so PII findings appear in the
security dashboard and can gate CI:

```bash
sqldoc scan --server localhost --database AdventureWorks2022 \
    --username sa --password '***' --sarif findings.sarif
```

**Custom patterns** — define org-specific sensitive-column categories in
`.sqldoc.yml` under `pii_patterns:` (checked before the built-in catalog). See
[`.sqldoc.example.yml`](.sqldoc.example.yml):

```yaml
pii_patterns:
  - category: "Employee ID"
    patterns: ['\bempid\b', 'employeenumber']
    severity: MEDIUM            # HIGH / MEDIUM / LOW
    regulations: ["Internal Policy"]
    action: "Restrict to HR systems."
    types: [varchar, nvarchar]  # optional; a matching type confirms
```

## How it works

`sqldoc` is a three-stage pipeline, one module per stage:

- `sqldoc/extractor.py` — queries the `sys.*` catalog views and builds `Table` /
  `Column` / `Index` / `View` / `StoredProcedure` dataclasses.
- `sqldoc/ai.py` — fills in descriptions via Ollama (local) or the Anthropic SDK
  (cloud), running the calls concurrently across a thread pool.
- `sqldoc/renderer.py` — renders the enriched data to a single HTML file.
