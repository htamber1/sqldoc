# Security

sqldoc is a **local-first** database tool: it reads only catalog/`sys.*` metadata
(and, for `quality`, aggregate statistics), never table row data, and nothing
leaves your network unless you explicitly opt into a cloud AI backend or an
integration. This document records the v3.0.0 security audit + hardening pass and
the project's ongoing security posture. The full security model, data boundary,
and enterprise-deployment guidance are in the sections below.

## Reporting a vulnerability

Please report security issues privately by opening a
[GitHub Security Advisory](https://github.com/htamber1/sqldoc/security/advisories/new)
(preferred), or by email to the maintainer. Do **not** open a public issue for an
unpatched vulnerability. We aim to acknowledge within 3 business days and ship a
fix or mitigation for confirmed issues promptly. See "CVE response process" below.

---

## Audit findings & responses (v3.0.0 hardening pass)

### 1. Dependency vulnerabilities (`pip-audit`)

Scanned with `pip-audit` against the full installed dependency tree.

| Package | Finding | In sqldoc's deps? | Response |
|---|---|---|---|
| (runtime deps) | **No known vulnerabilities** in anthropic / click / Jinja2 / pyodbc / python-dotenv / PyYAML / fpdf2 / requests and their transitive deps | — | Clean |
| `pip` | Several CVEs (PYSEC-2023-228, PYSEC-2026-*) | No — build tooling only, not shipped with the package | Upgraded the venv's `pip` to the patched release |
| `setuptools` | PYSEC-2022-43012, PYSEC-2025-49, PYSEC-2026-1918 | No — build tooling only, not a runtime dependency | Upgraded the venv's `setuptools` to the patched release |

**Hardening applied:** the dependency **lower bounds in `pyproject.toml` were
raised to the patched minimums** so a fresh install can never resolve a known-
vulnerable version:

- `Jinja2>=3.1.5` — fixes the autoescape/XSS advisories (CVE-2024-56201/-56326).
  sqldoc renders all HTML with `autoescape=True`, but the floor is raised anyway.
- `requests>=2.32.2` — fixes the `.netrc` credential-leak (CVE-2024-35195) and
  certificate-handling issues; pulls a patched `urllib3` + `certifi` transitively.
- `PyYAML>=6.0.1` — config parsing uses `yaml.safe_load` exclusively (never
  `yaml.load`), so untrusted YAML can't instantiate arbitrary objects.
- Every `requests`-based optional extra was bumped to `>=2.32.2` for consistency.

**Re-audit after upgrading the build tooling:** `No known vulnerabilities found`.

**Ongoing:** run `pip-audit` in CI on each change; treat any HIGH/CRITICAL in a
runtime dependency as a release blocker (see "CVE response process").

### 2. Static analysis (`bandit` + `semgrep`)

`bandit -r sqldoc` and `semgrep --config p/python --config p/security-audit`
(200 rules) over the whole codebase.

**Fixed — HIGH / MEDIUM:**

| Finding | Where | Fix |
|---|---|---|
| B324 insecure hash (SHA-1) | `ai.py` (2×) — AI description **cache keys** | Switched to `hashlib.sha256(..., usedforsecurity=False)`. Non-security content fingerprint; semgrep `insecure-hash-algorithm` also cleared. |
| B314 / B405 unsafe XML parse | `deadlocks.py`, `plans.py` (deadlock + query-plan XML) | Switched to **`defusedxml`** (now a core dependency) — hardened against XXE / billion-laughs even though the XML originates from the DB. |
| semgrep insecure-file-permissions | `hooks.py` — pre-commit hook chmod | Tightened to **owner-execute only** (`S_IXUSR`); dropped group/other execute bits. |

**semgrep after fixes: 0 findings.**

**Accepted — LOW severity (with justification):**

| Finding | Count | Justification |
|---|---|---|
| B110 try/except/pass, B112 try/except/continue | 37 | **Deliberate best-effort isolation** — a failing notification, optional AD probe, or cleanup must never crash the tool or a poll cycle. These are intentional; the primary path always propagates errors. |
| B404 / B603 subprocess | 3 | The agent daemon spawn, the git pre-commit hook, and the GitHub-Wiki push invoke subprocess with a **list of args and never `shell=True`** — no shell-injection surface; arguments are fixed subcommands + validated paths. |
| B607 partial executable path (`git`) | 1 | `git` is resolved from `PATH` by design (portability); pinning an absolute path would break normal installs. |
| B311 `random` | 1 | Used only for retry **backoff jitter**, never for tokens/secrets. Security-sensitive randomness uses `secrets` (e.g. approval tokens). |
| B105 "hardcoded password" | 1 | False positive — `"pass"` is a compliance-control **status**, not a credential (marked `# nosec B105`). |
| B608 hardcoded SQL expression | 26 | See **§4 SQL injection audit** — every site verified to interpolate only quoted identifiers or integer-cast values, never raw user input; identifiers can't be parameter-bound in T-SQL. |

**Ongoing:** run `bandit -ll` (MEDIUM+) and `semgrep p/python` in CI; new HIGH/MEDIUM findings block the build.

### 3. Secret scanning (`detect-secrets`)

Scanned the **working tree and the entire git history** (all reachable blobs
across every commit) for committed credentials, keys, tokens, and private keys.

**History scan** — every blob in `git rev-list --all` was grepped for
high-signal credential patterns (`sk-ant-…` Anthropic keys, `AKIA…` AWS keys,
`pypi-AgE…` PyPI tokens, `ghp_…`/`github_pat_…`/`gho_…` GitHub tokens, `xox…`
Slack tokens, `AIza…` Google API keys, and PEM private-key headers):
**no real secret has ever been committed.** Nothing to rotate.

**Working-tree scan** — `detect-secrets` flagged 20 tracked files; **all are
false positives**, audited and recorded in `.secrets.baseline`:
- Doc-string **connection-string examples** (`oracle://user:password@host`,
  `snowflake://user:password@account`, `postgresql://user:pw@host/db`) in the
  adapters / agent config docstrings and the VS Code extension placeholder.
- The Azure Container App Bicep template references secrets via `secretRef`
  (no literal secret values in source).
- **Test fixtures** — deliberately fake credentials in `tests/` (mock adapters,
  API-key auth tests, multi-tenant tests).

`.env` (which may hold a real `ANTHROPIC_API_KEY`) is **git-ignored and untracked**.

**Prevention added:**
- **`.secrets.baseline`** — an audited baseline of the known-safe placeholders.
- **`.pre-commit-config.yaml`** — a `detect-secrets` pre-commit hook (plus
  `detect-private-key` and large-file guards) that blocks any *new* secret from
  being committed. Enable with `pip install pre-commit && pre-commit install`.

**Ongoing:** the pre-commit hook gates local commits; re-audit the baseline
(`detect-secrets scan --baseline .secrets.baseline`) whenever a legitimate
high-entropy string is added.

### 4. SQL injection audit

Reviewed **every** dynamic SQL construction in the codebase (bandit flagged 26
B608 sites; each was manually verified). sqldoc is primarily a *read-only
metadata* tool, so most SQL is fixed catalog/DMV queries. Findings:

**Design principle — bound parameters wherever a value can be bound.** Row-value
filters use `?` / `%s` placeholders (e.g. the agent audit store binds `since` and
`limit`). The interpolated sites fall into three provably-safe classes:

1. **Integer-cast counts** — `TOP (n)` / `LIMIT n` / threshold comparisons are
   interpolated as `{int(...)}` / `{float(...)}`. T-SQL does **not** allow a bind
   parameter in `TOP`/`LIMIT`, and the cast makes injection impossible.
2. **Dialect-quoted catalog identifiers** — table/column/schema names come from
   the database's own catalog (a documentation tool reads existing schema), and
   are quoted with the close-quote **doubled** per dialect (`]`→`]]`, `"`→`""`,
   `` ` ``→`` `` ``). Identifiers cannot be bind parameters, so quoting is the
   correct defense. (`quality.py`, `pii.py`, `adapters/sqlite.py`, `intel.py`.)
3. **Fixed module constants** — e.g. the `waits` benign-wait ignore list.

**Two hardening fixes applied** (defense-in-depth on values that flow from
directory/config data into *generated* scripts, not executed by sqldoc):

- **`access/script.py`** — the check-then-create existence tests embed the login
  in a T-SQL **string literal** `N'…'`. Added `_lit()` (doubles single quotes) so
  a login name containing `'` cannot break out of the generated grant script.
  (Identifiers in the same script were already bracket-quoted via `_q()`.)
- **`access/intake.py`** — the Azure DevOps **WIQL** query embeds the tag in a
  string literal; now single-quote-escaped. (`integrations/azuredevops.py`
  already escaped its title the same way.)

Every reviewed site carries an inline `# nosec B608` with its justification, so a
*new* string-built query stands out in CI. **bandit after this pass: 0
HIGH/MEDIUM findings.**

### 5. Input validation

Added a **central validation layer** (`sqldoc/validation.py`) rather than ad-hoc
per-call-site checks. Every validator returns a normalized value or raises
`ValidationError` (a message safe to show the user, naming the offending field):

| Validator | Guards against |
|---|---|
| `validate_server` / `validate_database` / `validate_username` | **ODBC connection-string injection** — rejects `;{}=`, CR/LF, NUL and over-length values, while still accepting named instances (`host\instance`), `host,port`, Azure FQDNs, and `DOMAIN\user` / `user@domain`. |
| `validate_port` | non-integer / out-of-range ports. |
| `validate_output_path` | NUL bytes; optional `base_dir` **path-traversal** containment. |
| `validate_url` | non-allowlisted URL schemes / missing host. |
| `is_internal_host` / `assert_safe_outbound_url` | **SSRF** — loopback / link-local / private / reserved IP literals and cloud-metadata hosts (`169.254.169.254`, `metadata.google.internal`, …). |

**Wired in at the injection-prone sinks:**
- **`adapters/sqlserver.py` `build_connection_string`** now validates
  server/database/username and **brace-quotes the password** (`{…}`, closing
  brace doubled) so a value containing `;` can't inject extra ODBC attributes.
- **`cms.py connection_string_for`** (registered-server names come from config)
  gets the same validation + password brace-quoting.
- **`serve`** validates `--host` and `--port`.

The URL / SSRF validators back the API and network-hardening phases below.
Adding a new external input means calling a validator here, not writing a fresh
regex at the call site.

### 6. Credential handling

**Where credentials live and how they're protected:**
- The password / API key are supplied per run (CLI flag, `.sqldoc.yml`, or
  `ANTHROPIC_API_KEY` from `.env`) and held only in memory for the connection.
  `.env` and `.sqldoc.yml` are git-ignored.
- **No credential is ever logged or printed.** The `serve`/analysis paths never
  echo the connection string; error messages parse out the *database name* only,
  never the password. The `secure` scanner reports *that* a login has a blank/no
  password — it never prints password values.

**Audit-log redaction hardened** (`audit.py`):
- Redaction switched from three exact key names to **substring matching** over
  `password / passwd / pwd / secret / token / api_key / credential / webhook /
  private_key / connection_string`, so `bind_password`, `client_secret`,
  `smtp_password`, `access_token`, `webhook_url`, … are all redacted.
- Added **value-level** redaction: any string value that embeds a credential
  (`PWD=…`, or a `scheme://user:pass@host` URL) is replaced with
  `***redacted***` even under a benign key name.
- The audit still records *that* a secret was supplied (so the trail is complete)
  without storing its value.

**File-permission warning** — `load_config` now calls
`validation.warn_if_insecure_permissions`: on POSIX, if `.sqldoc.yml` is
group/other-readable it prints a `chmod 600` warning (best-effort, never fatal).
On Windows, NTFS ACLs govern access, so the check is a no-op. The same applies to
`~/.pypirc` used for publishing — keep it `chmod 600`; sqldoc never reads it.

### 7. REST API hardening (`sqldoc serve`)

Audited the stdlib HTTP API (`api.py`). It binds `127.0.0.1` by default, reads
only metadata, and authenticates with `X-API-Key` and/or SSO. Hardening applied:

| Area | Before | After |
|---|---|---|
| **Auth timing** | `provided == api_key` (string `==`, timing side-channel) | `hmac.compare_digest` constant-time (`_key_matches`). |
| **Auth bypass** | open when no key/SSO set (any bind) | still supported for localhost dev, but the CLI now prints a **red DANGER** warning when serving unauthenticated on a **non-loopback** address. |
| **Verbose errors** | `500 {"error": "<ExcType>: <msg>"}` leaked internals (paths, SQL) | generic `500 {"error": "internal server error"}`; full detail is logged **server-side** only. |
| **Security headers** | none | every response sends `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. |
| **CORS** | none | **intentionally none** — no `Access-Control-Allow-Origin`, so a browser on another origin cannot read these authenticated JSON responses. Documented so nobody adds a wildcard. |
| **Rate limiting** | none | per-client fixed-window `RateLimiter` (default 120 req/min), `429` when exceeded; `rate_limit=0` disables. |
| **Body size** | read `Content-Length` bytes unbounded | capped at 1 MiB; malformed/oversize → `413`. |

**Multi-tenant isolation** (already present, re-verified): the `X-API-Key`
selects the tenant, the tenant registry is stripped from the per-request context,
and `/api/agent/status` (operator data) is not exposed across tenants — a key can
only ever reach its own tenant's database.

SSRF via outbound webhook/integration URLs is addressed in **§10 Network
security** (the inbound API never fetches caller-supplied URLs).

### 8. Agent (background daemon) hardening

Reviewed `sqldoc/agent/` — the persistent poller daemon, its SQLite store, PID
handling, dashboard, and shutdown.

- **State-directory permissions** — `~/.sqldoc/` holds the SQLite store (schema
  + PII snapshots, the audit trail), the PID, and the log. `path_in_home` now
  `chmod 0700`s the directory on POSIX so other local users can't read
  monitoring data. (Windows: NTFS ACLs govern.)
- **Store concurrency (verified, not a leak)** — `AgentStore` opens a
  short-lived connection **per call** and runs in **WAL** mode, so the per-DB
  poller threads and the dashboard request threads never share a connection or
  block each other. No cross-thread cursor state → no data race.
- **Safe shutdown (verified)** — `run_daemon` blocks on `stop_event`, then
  `server.shutdown()`, sets every poller's stop event, `join(timeout=…)`s all
  threads, and `server_close()`s. `stop_event.wait(interval)` makes pollers wake
  immediately on stop. The `agent stop` CLI uses a stop-flag file plus a
  liveness-checked terminate fallback; `pid_alive` uses `OpenProcess` on Windows
  (never `os.kill(pid, 0)`, which *terminates* on Windows).
- **Memory-leak watchdog** — added an opt-in `memory_watch_loop`
  (`SQLDOC_AGENT_TRACEMALLOC=1`) that samples **tracemalloc** periodically and
  logs current/peak usage + the top growth since the last sample, so a slow leak
  in a long-running daemon surfaces in the log instead of an eventual OOM. Off by
  default (tracemalloc has per-allocation overhead).
- **Dashboard headers** — the localhost dashboard now sends `X-Content-Type-
  Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Cache-Control: no-store`
  and a CSP scoped to `'self' 'unsafe-inline'` (the pages embed inline CSS/SVG)
  that blocks framing, external script/connect, and object embeds. SSO gating on
  the dashboard is unchanged.

### 9. File handling

**Path traversal** — the only filenames built from *data* (rather than a
user-typed `--output`) are the cache / snapshot / baseline / executive-snapshot
files named after the **database**. These already route through `_safe_filename`,
which replaces every non-`[A-Za-z0-9-_.]` character (so `/` and `\` become `_`)
— traversal is impossible. Hardened further as defense-in-depth: it now strips
leading dots, collapses any `..` run, and never returns an empty component
(`..` → `db`, `../../etc/passwd` → `___etc_passwd`). A user-supplied `--output`
is intentionally free to write where the user chooses (it's their machine); the
`validate_output_path` helper (with a `base_dir`) is available for any future
path that comes from untrusted config/API input.

**Safe config parsing** — all YAML is parsed with **`yaml.safe_load`** (never
`yaml.load` / `unsafe_load` / `full_load`), so a malicious config **cannot
construct arbitrary Python objects or execute code**. Verified there is **no
`pickle.load`, `eval`, or `exec`** anywhere in the package. Malformed YAML now
fails with a clean error instead of an uncaught traceback: the main
`load_config`, the agent config loader, and `dbt_project.yml` catch `YAMLError`
and raise a usage error; per-file dbt `schema.yml` parsing already skipped bad
files with a warning.

### 10. Network security

Audited every outbound HTTP/network call (all `requests` usage, plus SMTP for
email alerts, plus the Ollama/Anthropic/OpenAI/Gemini AI backends).

- **TLS always verified** — there is **no `verify=False`** (or `CERT_NONE`,
  `check_hostname=False`) anywhere; `requests` verifies certificates by default
  and the SSRF helper explicitly refuses `verify=False`.
- **Timeouts everywhere** — every `requests` call passes an explicit `timeout`;
  the shared helper applies a 15 s default if a caller omits one, so no call can
  hang forever.
- **SSRF-safe redirects** — new `sqldoc/nethttp.py` `safe_request` follows
  redirects **manually** (`allow_redirects=False`) and vets every hop:
  a **cloud-metadata** host (`169.254.169.254`, `metadata.google.internal`, …)
  is refused on any hop, and a redirect that pivots from an **external** origin
  to an **internal** address is refused (the classic SSRF exfil path). Direct
  connections to a configured internal host stay allowed (self-hosted GitLab /
  Jira / Mattermost, or a localhost Ollama), so legitimate deployments aren't
  broken. The **least-trusted URL sinks — the generic webhook connector and the
  Slack/Teams webhooks — are routed through it.** (The inbound REST API never
  fetches caller URLs, closing the §7 SSRF item.)

### 11. Error handling

- **No tracebacks to end users** — the audit wrapper around every CLI command
  already caught unexpected exceptions (to record them); it now also **converts
  them to a clean, single-line error** instead of re-raising into a Python
  traceback. Set `SQLDOC_DEBUG=1` to get the full stack for debugging. The full
  error detail is still written to the audit log regardless. Verified there is no
  `traceback.print_exc` / `print_exc` anywhere in the package.
- **Errors caught at the right level** — connection failures surface as
  `Connection failed: …` + `Abort` (clean, actionable); per-check collectors in
  `health` / `server` / `secure` / `waits` / etc. isolate each check in its own
  `try/except` so one failing DMV degrades a single section, never the whole
  report; the REST API maps `ValueError` → `400` and everything else →
  `500 "internal server error"` (detail logged server-side only, §7).
- **Fail-safe + resource cleanup** — best-effort side-effects (notifications,
  audit writes, optional probes) are isolated so they can't crash the primary
  path; the adapters `close()` their DB connection after each extract, and the
  short-lived CLI process plus driver GC-on-drop bound any mid-error connection.
  The agent daemon shuts down by joining all threads and closing the server/store
  (§8).
