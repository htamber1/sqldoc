# sqldoc for VS Code

Document, PII/compliance-scan, and health-check your database without leaving
the editor. This extension drives the [`sqldoc`](https://github.com/htamber1/sqldoc)
CLI and renders its self-contained dark-themed HTML report in a VS Code webview.

## Features

Right-click a `.sql` file (or a folder, or use the Command Palette) and pick
**sqldoc →**:

- **Document This Database** — full schema documentation (ER diagram, tables,
  views, procedures).
- **Scan for PII** — HIPAA/GDPR/PCI-DSS PII/compliance scan.
- **Run Health Check** — DMV performance/health + unused-objects detector.
- **View Documentation** — open an existing `documentation.html` from the
  workspace, or generate one.

Each result opens in a webview panel using sqldoc's existing dark theme.

## Requirements

Install the CLI it drives:

```bash
pip install sqldoc            # + [postgres] / [mysql] / [all] for those engines
```

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `sqldoc.connectionString` | `""` | Connection string. If empty, the extension reads `connection_string` from a workspace `.sqldoc.yml`, then prompts. |
| `sqldoc.dialect` | `""` (auto) | `sqlserver`, `azuresql`, `postgres`, `mysql`, `sqlite`, `snowflake`, `oracle`. |
| `sqldoc.sqldocPath` | `sqldoc` | Path to the executable — e.g. `python -m sqldoc.cli`. |
| `sqldoc.documentArgs` | `["--no-ai"]` | Extra args for *Document This Database*. Remove `--no-ai` to enable AI descriptions (needs Ollama or a cloud key). |

## Privacy

Everything runs locally through the CLI. `scan` and `health` never leave your
machine; `doc` defaults to `--no-ai` here, and even with AI enabled only schema
metadata is sent (never row data). The rendered reports are fully self-contained
(no external CDN/font/image references), so the webview needs no network access.

## Install the packaged .vsix

```bash
code --install-extension sqldoc-vscode.vsix
```
