# LIVE_TESTING.md — validation checklist

The master checklist for validating sqldoc in a new environment. It lists **every
feature**, whether it is **live-validated** or **mock-only** today, and the
**exact command** to validate it yourself. Copy the checklist at the bottom into
an issue/PR when you bring up a new environment and tick features off as you
confirm them.

- **Live-tested** features are exercised against a real service in
  `tests/integration/` (the Docker sample DBs) or were live-smoke-validated and
  documented. They should "just work" — the command here re-confirms it.
- **Mock-only** features ship with unit/mock tests but have **not** run against a
  real service in CI (no account/license/credential available). Validate them
  with the `tests/live/` scripts — each command below points to the right one.

## Status legend

| Mark | Meaning |
|------|---------|
| ✅ **Live** | Validated against a real service (integration suite or documented live smoke). |
| 🧪 **Mock** | Unit/mock-tested only — validate with `tests/live/` when you have the service. |
| ⚪ **No-DB** | Deterministic, needs no live service — runs anywhere. |

## Set up the sample targets (for the live-tested commands)

The commands below use these shell variables. The Docker sample DBs match
`tests/integration/` (bring them up with `tests/integration/docker/setup_*.sh`).

```bash
export MSSQL='DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=AdventureWorks2022;UID=sa;PWD=SqlDoc123!;TrustServerCertificate=yes'
export PG='postgresql://postgres:sqldoc@localhost:55432/pagila'
export MYSQL='mysql://root:sqldoc@localhost:33061/sakila'
export SQLITE='chinook.db'          # any SQLite file
```

Run the whole live-tested surface at once with the existing suite:

```bash
pytest -m integration -v            # every live-DB test (auto-skips if a DB is down)
pytest tests/live -v                # every mock-only feature you've configured (see below)
```

---

## Commands (23 top-level)

All commands support `--json` and (HTML emitters) `--verify-offline`. Add
`--output <file>` to save the report.

| Command | Status | Live on | Exact validation command |
|---|---|---|---|
| `doc` | ✅ Live | SS, PG, MySQL, SQLite | `sqldoc doc --connection-string "$MSSQL" --dialect sqlserver --no-ai --output doc.html --verify-offline` |
| `scan` (PII) | ✅ Live | SS, PG, MySQL, SQLite | `sqldoc scan --connection-string "$PG" --dialect postgres --json scan.json` |
| `scan-files` | ⚪ No-DB | — | `sqldoc scan-files path/to/migration.sql --fail-on high` |
| `install-hooks` | ⚪ No-DB | — | `sqldoc install-hooks` (run in a git repo; then `git commit` a `.sql` with an `ssn` column) |
| `health` | ✅ Live | SS, PG, MySQL | `sqldoc health --connection-string "$MSSQL" --dialect sqlserver --json health.json` |
| `quality` | ✅ Live | SS, PG, MySQL, SQLite | `sqldoc quality --connection-string "$MYSQL" --dialect mysql --yes --json q.json` |
| `intel` | ✅ Live | SS, PG, MySQL, SQLite | `sqldoc intel --connection-string "$MSSQL" --dialect sqlserver --json intel.json` |
| `insights` | ✅ Live (heuristics) | SS, PG, MySQL | `sqldoc insights --connection-string "$PG" --dialect postgres --no-ai --json ins.json` (AI parts need Ollama/cloud — see AI backends) |
| `comply` | ✅ Live | SS, PG, MySQL | `sqldoc comply --connection-string "$MSSQL" --dialect sqlserver --json comply.json` |
| `executive` | ✅ Live | SS (family) | `sqldoc executive --connection-string "$MSSQL" --dialect sqlserver --no-baseline --output exec.html` |
| `dbt` | ⚪ No-DB | — | `sqldoc dbt --project-dir path/to/dbt_project --no-db --json dbt.json` |
| `server` | ✅ Live | SS | `sqldoc server --connection-string "$MSSQL" --dialect sqlserver --json server.json` |
| `logs` | ✅ Live | SS | `sqldoc logs --connection-string "$MSSQL" --dialect sqlserver --last-hours 72 --json logs.json` |
| `secure` | ✅ Live | SS (PG/MySQL supported) | `sqldoc secure --connection-string "$MSSQL" --dialect sqlserver --json sec.json` |
| `backup` | ✅ Live | SS | `sqldoc backup --connection-string "$MSSQL" --dialect sqlserver --json backup.json` |
| `waits` | ✅ Live | SS (PG/MySQL supported) | `sqldoc waits --connection-string "$MSSQL" --dialect sqlserver --no-ai --json waits.json` |
| `ha` | ✅ Live | SS (PG/MySQL supported) | `sqldoc ha --connection-string "$MSSQL" --dialect sqlserver --json ha.json` |
| `deadlocks` | ✅ Live | SS | `sqldoc deadlocks --connection-string "$MSSQL" --dialect sqlserver --no-ai --output dl.html` |
| `plans` | ✅ Live | SS (PG/MySQL supported) | `sqldoc plans --connection-string "$MSSQL" --dialect sqlserver --no-ai --json plans.json` |
| `capacity` | ✅ Live | via agent history | Run the agent for ≥2 cycles, then `sqldoc capacity --json cap.json` |
| `baseline` | ✅ Live | SS (PG/MySQL supported) | `sqldoc baseline --connection-string "$MSSQL" --dialect sqlserver --capture` then re-run to compare |
| `audit` | ⚪ No-DB | local log | `sqldoc audit --summary` (after running any command) |
| `serve` (REST API) | ✅ Live | SS | `sqldoc serve --connection-string "$MSSQL" --dialect sqlserver --api-key testkey` then `curl -H "X-API-Key: testkey" localhost:8090/api/doc` |

> Capability note: `doc`/`scan`/`intel`/`insights` are dialect-neutral (run on
> every adapter). `health`/`quality`/`comply` run on SQL Server + PostgreSQL +
> MySQL. `server`/`logs`/`deadlocks`/`plans`/`waits`/`ha`/`backup`/`secure`/
> `capacity`/`baseline` are SQL-Server-family features; several also run on
> PG/MySQL and degrade cleanly elsewhere.

---

## Command groups

### `sqldoc agent` — background monitoring daemon
| Feature | Status | Command |
|---|---|---|
| poll / store / dashboard / schema-change | ✅ Live | `sqldoc agent start --foreground` (configure `agent:` in `.sqldoc.yml`), then open `http://localhost:8080` |
| server/backup/HA/tempdb monitoring, NL alerts, weekly digest | ✅ Live (SS) / 🧪 (email digest delivery) | driven by `agent:` config; alert delivery validated via `tests/live/test_notifications_live.py` |

### `sqldoc access` — AD-aware access requests
| Subcommand | Status | Command |
|---|---|---|
| `access check` | ✅ Live (SQL side) | `sqldoc access check --user someone@corp.com` (directory side is 🧪 — see Identity providers) |
| `access request` | ✅ Live (SQL side) | `sqldoc access request --user u --request "read access to Sales"` |
| `access script` | ✅ Live (valid T-SQL) | `sqldoc access script --user u --database Sales --level read` |
| `access review` | ✅ Live | `sqldoc access review` |
| `access recommend` | 🧪 Mock | `sqldoc access recommend --user u` (needs peer data) |
| `access approve` | 🧪 Mock | approval store + email links |
| `access jira` | 🧪 Mock | needs a live Jira — `tests/live` (jira section) |
| `access intake` / `parse-email` | 🧪 Mock | needs ServiceNow/ADO/GitHub — `tests/live` |
| `access execute` | 🧪 Mock | runs generated T-SQL against a live server (validate manually with care) |

### `sqldoc cms` — Central Management Server (estate-wide)
| Feature | Status | Command |
|---|---|---|
| `cms discover` | 🧪 **Mock only** | `sqldoc cms discover --server "$CMS"` — the one CMS path unit-tested only; **validate against a real CMS** (reads msdb `sysmanagement_*`) |
| `cms report` | ✅ Live | `sqldoc cms report --config .sqldoc.yml` (with `cms_servers:`) |
| `--cms` bulk (doc/scan/health/quality/intel/comply/server/secure/backup) | ✅ Live | `sqldoc health --cms --config .sqldoc.yml` |
| `executive --cms`, `access review --cms` | ✅ Live | `sqldoc executive --cms` |

---

## Database dialects (17)

`doc`/`scan`/`intel`/`insights` run on all of them; see the capability note above
for the analysis commands.

| Dialect | Status | Validate with |
|---|---|---|
| SQL Server | ✅ Live (AdventureWorks2022) | `sqldoc doc --connection-string "$MSSQL" --dialect sqlserver --no-ai` |
| PostgreSQL | ✅ Live (Pagila) | `sqldoc doc --connection-string "$PG" --dialect postgres --no-ai` |
| MySQL | ✅ Live (Sakila) | `sqldoc doc --connection-string "$MYSQL" --dialect mysql --no-ai` |
| SQLite | ✅ Live (Chinook) | `sqldoc doc --connection-string "$SQLITE" --dialect sqlite --no-ai` |
| Azure SQL | 🧪 Mock | `SQLDOC_TEST_AZURESQL=… pytest tests/live -k azuresql` |
| Azure SQL Managed Instance | 🧪 Mock | `SQLDOC_TEST_AZURE_MI=… pytest tests/live -k azure_managed_instance` |
| Azure Synapse | 🧪 Mock | `SQLDOC_TEST_SYNAPSE=… pytest tests/live -k synapse` |
| Snowflake | 🧪 Mock | `SQLDOC_TEST_SNOWFLAKE=… pytest tests/live -k snowflake` |
| Oracle | 🧪 Mock | `SQLDOC_TEST_ORACLE=… pytest tests/live -k oracle` |
| Amazon Redshift | 🧪 Mock | `SQLDOC_TEST_REDSHIFT=… pytest tests/live -k redshift` |
| Databricks | 🧪 Mock | `SQLDOC_TEST_DATABRICKS=… pytest tests/live -k databricks` |
| Google BigQuery | 🧪 Mock | `SQLDOC_TEST_BIGQUERY=… pytest tests/live -k bigquery` |
| CockroachDB | 🧪 Mock | `SQLDOC_TEST_COCKROACHDB=… pytest tests/live -k cockroachdb` |
| IBM Db2 | 🧪 Mock | `SQLDOC_TEST_DB2=… pytest tests/live -k db2` |
| MongoDB | 🧪 Mock | `SQLDOC_TEST_MONGODB=… pytest tests/live -k mongodb` |
| Aurora PostgreSQL | 🧪 Mock | `SQLDOC_TEST_AURORA_PG=… pytest tests/live -k aurora_postgres` |
| Aurora MySQL | 🧪 Mock | `SQLDOC_TEST_AURORA_MYSQL=… pytest tests/live -k aurora_mysql` |

→ `tests/live/test_dialects_live.py` (connection-string format for each is in its docstring).

---

## AI backends (4)

| Backend | Status | Validate with |
|---|---|---|
| Ollama (local) | ✅ Live | `SQLDOC_TEST_OLLAMA=1 pytest tests/live -k ollama` (local Ollama at :11434) |
| Anthropic (cloud) | ✅ Live | `ANTHROPIC_API_KEY=… pytest tests/live -k anthropic` |
| OpenAI | 🧪 Mock | `OPENAI_API_KEY=… pytest tests/live -k openai` |
| Gemini | 🧪 Mock | `GOOGLE_API_KEY=… pytest tests/live -k gemini` |

End-to-end through a command, e.g.:
`sqldoc doc --connection-string "$MSSQL" --dialect sqlserver --ai-backend openai --yes`

→ `tests/live/test_ai_backends_live.py`

---

## Publishing / ticketing integrations (16) — all 🧪 Mock

Each has a `--test` mode (auth + connectivity, no DB). Fill in its section in
`tests/live/sqldoc.live.yml`, then run the command (or the script).

| Integration | Validate with |
|---|---|
| SharePoint | `sqldoc sharepoint --test` |
| Confluence | `sqldoc confluence --test` |
| Notion | `sqldoc notion --test` |
| Google Drive | `sqldoc gdrive --test` |
| Box | `sqldoc box --test` |
| Jira | `sqldoc jira --test` |
| ServiceNow | `sqldoc servicenow --test` |
| Azure DevOps | `sqldoc azuredevops --test` |
| Power BI | `sqldoc powerbi --test` |
| Generic webhook | `sqldoc webhook --test` |
| GitHub Wiki | `sqldoc github-wiki --test` |
| GitLab Wiki | `sqldoc gitlab-wiki --test` |
| Azure DevOps Wiki | `sqldoc azuredevops-wiki --test` |
| OneDrive | `sqldoc onedrive --test` |
| Dropbox | `sqldoc dropbox --test` |
| Nuclino | `sqldoc nuclino --test` |

All at once: `pytest tests/live/test_integrations_live.py -v`. For a real publish:
`sqldoc <connector> --push --connection-string "$MSSQL" --dialect sqlserver`.
→ `tests/live/test_integrations_live.py`

---

## Notification channels — 🧪 Mock (Slack live-smoked)

Fill in `live_notify:` in the live config. **These send real messages.**

| Channel | Validate with |
|---|---|
| Slack | `pytest tests/live/test_notifications_live.py -k slack` |
| Microsoft Teams | `… -k teams` |
| Cisco Webex | `… -k webex` |
| Email (SMTP) | `… -k email` |
| Twilio SMS | `… -k twilio` |
| WhatsApp | `… -k whatsapp` |
| PagerDuty | `… -k pagerduty` (resolve the test alert after) |
| OpsGenie | `… -k opsgenie` (close the test alert after) |

→ `tests/live/test_notifications_live.py`

---

## `sqldoc access` identity providers — 🧪 Mock

The SQL side of `access` is live; the **directory** side is mock-only. Set
`access.ad` in the live config (its `type` picks the provider) and
`SQLDOC_TEST_AD_USER`.

| Provider | `access.ad.type` |
|---|---|
| Generic LDAP | `ldap` / `generic-ldap` |
| Microsoft Entra ID (Graph) | `graph` |
| Hybrid AD (Graph + on-prem login) | `hybrid` |
| Okta | `okta` |
| Google Workspace | `google` |
| JumpCloud | `jumpcloud` |

Validate: `SQLDOC_TEST_AD_USER=user@corp.com pytest tests/live/test_identity_live.py -v`
→ `tests/live/test_identity_live.py`

---

## New-environment validation checklist

Copy this into your environment bring-up ticket and tick as you confirm. Only the
rows relevant to that environment need doing.

```
Databases
  [ ] SQL Server            sqldoc doc --connection-string "$MSSQL" --dialect sqlserver --no-ai
  [ ] PostgreSQL            sqldoc doc --connection-string "$PG" --dialect postgres --no-ai
  [ ] MySQL                 sqldoc doc --connection-string "$MYSQL" --dialect mysql --no-ai
  [ ] SQLite                sqldoc doc --connection-string "$SQLITE" --dialect sqlite --no-ai
  [ ] Azure SQL             pytest tests/live -k azuresql
  [ ] Azure SQL MI          pytest tests/live -k azure_managed_instance
  [ ] Synapse               pytest tests/live -k synapse
  [ ] Snowflake             pytest tests/live -k snowflake
  [ ] Oracle                pytest tests/live -k oracle
  [ ] Redshift              pytest tests/live -k redshift
  [ ] Databricks            pytest tests/live -k databricks
  [ ] BigQuery              pytest tests/live -k bigquery
  [ ] CockroachDB           pytest tests/live -k cockroachdb
  [ ] Db2                   pytest tests/live -k db2
  [ ] MongoDB               pytest tests/live -k mongodb
  [ ] Aurora PostgreSQL     pytest tests/live -k aurora_postgres
  [ ] Aurora MySQL          pytest tests/live -k aurora_mysql

Core commands (against the primary DB)
  [ ] doc  [ ] scan  [ ] health  [ ] quality  [ ] intel  [ ] insights  [ ] comply
  [ ] executive  [ ] server  [ ] logs  [ ] secure  [ ] backup  [ ] waits  [ ] ha
  [ ] deadlocks  [ ] plans  [ ] baseline  [ ] serve  [ ] agent (dashboard)

AI backends
  [ ] Ollama  [ ] Anthropic  [ ] OpenAI  [ ] Gemini

Estate (if a CMS is present)
  [ ] cms discover (real msdb)   [ ] cms report   [ ] --cms bulk   [ ] executive --cms

Integrations (only those in use)
  [ ] SharePoint [ ] Confluence [ ] Notion [ ] Google Drive [ ] Box [ ] Jira
  [ ] ServiceNow [ ] Azure DevOps [ ] Power BI [ ] webhook [ ] GitHub Wiki
  [ ] GitLab Wiki [ ] Azure DevOps Wiki [ ] OneDrive [ ] Dropbox [ ] Nuclino

Notifications (only those in use)
  [ ] Slack [ ] Teams [ ] Webex [ ] Email [ ] Twilio SMS [ ] WhatsApp
  [ ] PagerDuty [ ] OpsGenie

Identity (if using `sqldoc access`)
  [ ] LDAP / AD (Graph/hybrid) / Okta / Google Workspace / JumpCloud
```
