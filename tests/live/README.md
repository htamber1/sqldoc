# tests/live — validate mock-only features against real services

`tests/integration/` validates the core product against the local Docker sample
databases (SQL Server / PostgreSQL / MySQL). **This** directory validates the
features that ship **mock-tested only** because they need an account, license, or
SaaS credential we don't have in CI:

- **Cloud/enterprise database adapters** — Snowflake, Oracle, Redshift,
  Databricks, BigQuery, CockroachDB, Db2, MongoDB, Azure SQL / MI / Synapse,
  Aurora PG/MySQL.
- **AI backends** — OpenAI and Gemini (Ollama + Anthropic are already live).
- **Publishing / ticketing integrations** — SharePoint, Confluence, Notion,
  Google Drive, Box, Jira, ServiceNow, Azure DevOps, Power BI, generic webhook,
  GitHub/GitLab/Azure DevOps wikis, OneDrive, Dropbox, Nuclino.
- **Notification channels** — Slack, Teams, Webex, email, Twilio SMS, WhatsApp,
  PagerDuty, OpsGenie.
- **`sqldoc access` identity providers** — LDAP, Microsoft Graph / hybrid AD,
  Okta, Google Workspace, JumpCloud.

Every check is **skip-gated**: it runs only when you supply the credential and
skips otherwise. `pytest tests/live` is safe to run with nothing configured
(everything skips) and safe to commit alongside the code.

## How to run

Credentials come from two places:

1. **Env vars** — database connection strings and AI keys:
   `SQLDOC_TEST_SNOWFLAKE`, `SQLDOC_TEST_ORACLE`, … (see
   `test_dialects_live.py` for the full list), plus `OPENAI_API_KEY`,
   `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `SQLDOC_TEST_OLLAMA=1`,
   `SQLDOC_TEST_AD_USER`.
2. **A live config file** — a normal `.sqldoc.yml` with the integration /
   notification / identity sections filled in. Copy the template and edit:

```bash
cp tests/live/sqldoc.live.example.yml tests/live/sqldoc.live.yml
# edit sqldoc.live.yml (it is git-ignored — it holds real secrets)
```

Then:

```bash
# Everything you've configured (the rest skips):
pytest tests/live -v

# One area:
pytest tests/live/test_dialects_live.py -v
pytest tests/live/test_integrations_live.py -v
pytest tests/live/test_notifications_live.py -v   # sends REAL messages
pytest tests/live/test_ai_backends_live.py -v
pytest tests/live/test_identity_live.py -v

# A single service (pytest -k):
SQLDOC_TEST_SNOWFLAKE="snowflake://user:pw@acct/DB/SCHEMA?warehouse=WH" \
  pytest tests/live -k snowflake -v

# Point at a config elsewhere:
SQLDOC_LIVE_CONFIG=/secure/sqldoc.live.yml pytest tests/live -v
```

All live tests carry the `live` marker, so `pytest -m live` selects them and
`pytest -m "not live"` excludes them.

## What "passing" means

- **Dialects** — `sqldoc doc`/`scan` extract real metadata from the live instance
  (asserts ≥1 table/collection came back).
- **AI backends** — `ai.dispatch` gets a non-empty completion from the provider.
- **Integrations** — `sqldoc <connector> --test` authenticates and reaches the
  service. (For a real publish, run `sqldoc <connector> --push …` manually.)
- **Notifications** — a "sqldoc live test" message actually arrives; check the
  destination, and resolve/close any PagerDuty/OpsGenie test alert afterwards.
- **Identity** — the directory resolves a real user (`SQLDOC_TEST_AD_USER`) with
  their groups.

## Notes

- The scripts import a shared helper as `from _liveutil import …`; pytest puts
  this directory on `sys.path`, so run them **through pytest** (not as bare
  `python test_*.py`).
- `sqldoc.live.yml` is git-ignored. Never commit real credentials.
- Record results in **`LIVE_TESTING.md`** (repo root) as you validate each
  feature in a new environment — that file is the master checklist.
