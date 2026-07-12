# sqldoc GitHub Action

Run [`sqldoc`](https://github.com/htamber1/sqldoc) in your CI pipeline to
**document**, **PII/compliance-scan**, or **health-check** a database and
publish a self-contained HTML report — local-first, no data leaves the runner
unless you explicitly opt into a cloud AI model.

## Usage

```yaml
name: Database docs
on: [workflow_dispatch]

jobs:
  sqldoc:
    runs-on: ubuntu-latest
    steps:
      - uses: htamber1/sqldoc-action@v1
        with:
          command: doc
          connection-string: ${{ secrets.DB_CONNECTION_STRING }}
          dialect: postgres
          output-path: db-docs.html
          extra-args: --no-ai          # CI usually has no local Ollama
      - uses: actions/upload-artifact@v4
        with:
          name: db-docs
          path: db-docs.html
```

### PII scan that fails the build on HIGH-risk findings

```yaml
      - uses: htamber1/sqldoc-action@v1
        with:
          command: scan
          connection-string: ${{ secrets.DB_CONNECTION_STRING }}
          dialect: sqlserver
          output-path: pii-report.html
          json-output: pii-report.json
          fail-on-high-pii: 'true'
```

### Health check

```yaml
      - uses: htamber1/sqldoc-action@v1
        with:
          command: health
          connection-string: ${{ secrets.DB_CONNECTION_STRING }}
          dialect: postgres
          output-path: health.html
```

## Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `command` | `doc` | `doc`, `scan`, or `health`. |
| `connection-string` | — (required) | Connection/ODBC string. **Always use a secret.** |
| `dialect` | auto-detect | `sqlserver`, `azuresql`, `postgres`, `mysql`, `sqlite`, `snowflake`, `oracle`. Selecting one also installs the matching driver extra. |
| `output-path` | `sqldoc-report.html` | Where to write the HTML report. |
| `fail-on-high-pii` | `false` | `scan` only — fail the job if any HIGH-risk PII is found (maps to `--fail-on high`). |
| `json-output` | — | Also write a machine-readable JSON report (`scan`/`health`). |
| `extra-args` | — | Passed through verbatim, e.g. `--no-ai`, `--schemas dbo,Sales`, `--sample`. |
| `sqldoc-version` | latest | Pin a specific PyPI version. |
| `python-version` | `3.11` | Python to set up. |

## Outputs

| Output | Description |
|--------|-------------|
| `report-path` | Path to the generated HTML report. |
| `json-path` | Path to the JSON report (empty if not requested). |

A full, copy-pasteable workflow lives in [`example-workflow.yml`](./example-workflow.yml).

## Two ways to reference this action

- **Published (recommended):** `uses: htamber1/sqldoc-action@v1` — the manifest
  in this directory is mirrored to the standalone
  [`htamber1/sqldoc-action`](https://github.com/htamber1/sqldoc-action) repo and
  tagged `v1` (see `publish-action.sh`). Requires no repo checkout in the
  consumer job.
- **Local path:** `uses: ./.github/actions/sqldoc` — from a job in *this* repo,
  after `actions/checkout@v4`.

## Privacy

`doc` in CI runs with `--no-ai` (or a cloud model if you pass `--mode cloud`
plus `ANTHROPIC_API_KEY`). Even with cloud AI, only schema **metadata** is sent
off the runner — never table row data. `scan` and `health` are fully local.
