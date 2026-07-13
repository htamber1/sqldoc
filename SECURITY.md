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

<!-- Subsequent audit sections (static analysis, secrets, SQL injection, input
     validation, credentials, API, agent, files, network, errors) are appended by
     the corresponding hardening commits. -->
