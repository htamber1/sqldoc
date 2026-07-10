# sqldoc

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
   doesn't already have one. Enrichment runs concurrently (see `--concurrency`).
3. **Renders** a standalone HTML document — grouped by schema, with an ER diagram,
   real-time search, and collapsible view/procedure definitions, styled inline,
   no external assets or dependencies to serve.

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
# from the repo root
python -m venv venv
venv/Scripts/activate        # Windows;  source venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

For cloud mode, create a `.env` file in the repo root with your API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

Run as a module from the repo root:

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

### Options

| Option | Description |
| --- | --- |
| `--server` | SQL Server hostname or IP (**required**) |
| `--database` | Database name to document (**required**) |
| `--username` | SQL Server username (**required**) |
| `--password` | SQL Server password (**required**) |
| `--output` | Output HTML file path (default `documentation.html`) |
| `--mode local\|cloud` | AI backend: `local` (Ollama, default) or `cloud` (Anthropic) |
| `--model` | Model to use. Defaults per mode: `llama3.1:8b` (local), `claude-haiku-4-5` (cloud) |
| `--schemas` | Comma-separated list of schemas to include (default: all) |
| `--no-ai` | Skip AI descriptions, output schema only |
| `--concurrency` | Parallel AI calls during enrichment, 1-64 (default `8`) |
| `--config` | Path to a config file (default `.sqldoc.yml` if present) |
| `--yes` / `-y` | Skip the cloud-mode confirmation prompt (for non-interactive/CI use) |

The connection flags are marked required above, but any of them (and every other
option) can instead be supplied from a config file — see below.

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

## How it works

`sqldoc` is a three-stage pipeline, one module per stage:

- `sqldoc/extractor.py` — queries the `sys.*` catalog views and builds `Table` /
  `Column` / `Index` / `View` / `StoredProcedure` dataclasses.
- `sqldoc/ai.py` — fills in descriptions via Ollama (local) or the Anthropic SDK
  (cloud), running the calls concurrently across a thread pool.
- `sqldoc/renderer.py` — renders the enriched data to a single HTML file.
