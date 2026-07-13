# Testing sqldoc

sqldoc ships a comprehensive, layered test suite: fast deterministic **unit/mock**
tests that run anywhere, **live integration** tests that run against real
databases when they're reachable (and skip cleanly otherwise), a **regression**
contract suite, and **performance** checks.

```
venv/Scripts/python.exe -m pytest -q                         # everything (integration auto-skips if no DB)
venv/Scripts/python.exe -m pytest tests --ignore=tests/integration   # unit/mock + regression only
venv/Scripts/python.exe -m pytest -m integration             # only the live-DB tests
venv/Scripts/python.exe -m pytest -m regression              # only the pinned-contract suite
```

## Test inventory

| Layer | Count | Needs live DB? | Location |
|---|---|---|---|
| Unit / mock | ~1118 | No | `tests/test_*.py` |
| Regression (pinned contracts) | 10 | No | `tests/regression/` |
| Integration (live e2e) | 60+ | Yes (skip-gated) | `tests/integration/` |
| **Total** | **1190+** | | |

> Counts grow with each release; run `pytest --collect-only -q | tail -1` for the
> exact current number. The CMS suite (discovery, bulk, executive, agent mode,
> inventory report, estate access audit, change digest) and its live pipeline
> tests are included.

All 1111 pass. The unit + regression layers (1057 tests) run on any machine with
no database, Docker, or network. The 54 integration tests **skip automatically**
when their target database isn't reachable, so `pytest` is always green.

## Coverage

Measured with `coverage run --source=sqldoc -m pytest` (see `.coveragerc`):

- **89.5% overall line coverage** with the live integration tests running.
- Unit/mock tests alone reach ~88%; the integration tests add live coverage of
  the orchestration and dialect-specific SQL that can't be exercised with mocks.

Coverage by tier of the codebase:

- **≥95% (most modules)** — the collectors (`health`, `secure`, `backup`,
  `comply`, `quality`, `server`, `waits`, `plans`, `deadlocks`, `executive`,
  `frameworks`), every renderer, the PII engine, the adapters, the access-suite
  logic (`script`, `login_types`, `roles`, `gap`, `parse`, `checker`, `review`,
  `recommend`), and the integration connectors.
- **80–90%** — `cli.py` (81% — the large orchestration layer; the remaining
  lines are per-command echo/error branches, heavily exercised live by the
  integration suite), `api.py`, `ai.py`, `agent/alerting.py`, `access/intake.py`.
- **Lower** — `agent/cli.py` (58% — the daemon *start/stop/spawn* control flow
  detaches a background process, which the in-process test harness deliberately
  doesn't spawn; the daemon's actual work is covered via `poll_database` and the
  loop functions), and the live LLM backend calls in `ai.py` (Ollama/Anthropic/
  OpenAI/Gemini HTTP bodies — see "requires live credentials" below).

Regenerate:

```bash
venv/Scripts/python.exe -m coverage run --source=sqldoc -m pytest -q
venv/Scripts/python.exe -m coverage report --sort=cover
venv/Scripts/python.exe -m coverage html        # -> htmlcov/index.html
```

## What the integration tests cover

Provision the databases (Docker) with the helper scripts, then run
`pytest -m integration`:

```bash
# SQL Server: use your own container (localhost, sa / SqlDoc123!, AdventureWorks2022)
bash tests/integration/docker/setup_postgres.sh   # postgres:16 + Pagila  -> localhost:55432
bash tests/integration/docker/setup_mysql.sh      # mysql:8   + Sakila  -> localhost:33061
```

Connection targets are overridable via `SQLDOC_TEST_MSSQL` / `SQLDOC_TEST_PG` /
`SQLDOC_TEST_MYSQL`.

- **`test_sqlserver.py` (18)** — every SQL-Server-facing command end-to-end
  against AdventureWorks2022: doc (HTML+JSON), scan, health, quality, intel,
  insights, comply, server, logs, secure, waits, ha, deadlocks, plans, executive,
  baseline (capture+compare). Each validates exit code + HTML/JSON content.
- **`test_postgres.py` (8)** — the dialect-neutral + PG command suite against
  Pagila (doc, scan+PII, health, quality, intel, insights, comply).
- **`test_mysql.py` (8)** — the same suite against Sakila.
- **`test_cross_dialect.py` (9)** — the same command produces structurally
  identical output (identical JSON keys and HTML class-structure) across SQL
  Server / PostgreSQL / MySQL; only the values differ.
- **`test_agent.py` (4)** — the agent poll cycle against a fresh isolated
  database: store population, dashboard routes, **schema-change detection**
  (ALTER TABLE between polls), and capacity from agent history.
- **`test_access.py` (4)** — access check / request / script / review against a
  real created SQL login; the generated grant + rollback scripts are validated
  as **valid T-SQL** via `SET PARSEONLY` on the live server.
- **`test_performance.py` (3)** — `doc` on the 71-table database completes well
  under the 60s budget (~1s observed), no memory leak across 5 runs (steady-state
  RSS growth < 40 MB), and doc+scan throughput within budget.

## Requires live credentials (not tested automatically)

These paths are exercised only with mocked transports (unit tests confirm the
request shaping, auth headers, error handling, and missing-credential guards);
end-to-end validation needs real accounts:

- **AI backends** — Ollama (local server), Anthropic / OpenAI / Google Gemini
  (API keys). The `ai.dispatch` routing, retry, and per-backend model defaults
  are unit-tested; the actual HTTP calls are not.
- **Identity providers** — Okta, Google Workspace, JumpCloud, and Entra ID /
  Graph. LDAP/AD is validated only against the shape of directory responses.
- **Integrations** — SharePoint, Confluence, Notion, Google Drive, Box, Jira,
  ServiceNow, Azure DevOps, Power BI, the wikis, OneDrive, Dropbox, Nuclino, and
  the notification channels (Slack, Teams, Webex, PagerDuty, OpsGenie, Twilio,
  WhatsApp). All are mock-tested against their real API contracts.
- **SSO** — OIDC / SAML validation logic is unit-tested with injected verifiers;
  a real IdP round-trip is not.
- **Cloud / container deployment** — the Dockerfile, Helm chart, and Azure/AWS/
  GCP templates are structure-validated but not deployed by the suite (no
  `helm`/`az`/`aws`/`terraform`/live cluster in the harness).

## Known limitations

- **Snowflake, Oracle, and the other cloud-warehouse adapters** are mock-tested
  only (they need a licensed/live account); the SQL Server / PostgreSQL / MySQL /
  SQLite adapters are live-validated.
- **Inactivity detection** in `access review` uses `sys.dm_exec_sessions`, which
  only remembers current/recent sessions — accurate history needs a logon audit.
- **`agent/cli.py`** daemon spawn/stop is intentionally not exercised in-process
  (it detaches a real background process); the daemon's polling, dashboard, and
  loop logic are covered directly.
- Integration tests mutate the live server minimally (create/drop a temp database
  or login, ALTER a temp table) and always clean up on teardown.
