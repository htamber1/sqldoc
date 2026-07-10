# sqldoc — Pricing & Tiering Strategy

> Working strategy document. Dollar figures for competitors are approximate
> market positioning as of this writing and should be re-verified before any
> figures are published externally.

## Positioning

sqldoc spans two jobs that are usually sold separately:

1. **Database documentation** — what Redgate SQL Doc, ApexSQL Doc, dbForge
   Documenter, and Dataedo do.
2. **PII discovery & compliance reporting** — what data-classification tools
   (Microsoft Purview, BigID, Immuta, Satori, Dataedo's classification add-on)
   do, typically at a much higher price point.

Owning both lets us anchor the documentation tiers against cheap, well-understood
competitors, and price the compliance tier against a category that customers are
used to paying *far* more for. The result is a ladder where each rung is an easy
"yes" relative to the alternative.

## Pricing model: per-server, per-month subscription

We price **per SQL Server instance per month**, not per user.

- **It matches value.** Documentation and compliance risk scale with the number
  of database servers under management, not with how many people read the docs.
- **It fits how the tool runs.** sqldoc is a CLI built for automation (CI jobs,
  scheduled scans). Per-seat pricing punishes exactly the unattended, org-wide
  usage we want to encourage; per-server rewards it.
- **It's simple to meter and forecast.** A customer knows their server count;
  there's no seat-counting friction or true-up anxiety.
- **It lands cheaper for teams.** Competitors' per-user perpetual licenses
  ($300–500/user) get expensive across a data team; a per-server subscription is
  predictable and usually lower total cost for the buyer while giving us
  recurring revenue.

Annual billing at ~2 months free (≈17% discount) is offered on all paid tiers.

## Tier summary

| | **Free** | **Professional** | **Compliance** | **Enterprise** |
|---|---|---|---|---|
| **Price** | $0 | **$49 / server / mo** | **$149 / server / mo** | Custom |
| Documentation (HTML) | ✓ | ✓ | ✓ | ✓ |
| Local AI descriptions (Ollama) | ✓ | ✓ | ✓ | ✓ |
| Cloud AI descriptions (Anthropic) | — | ✓ | ✓ | ✓ |
| PDF & Markdown export | — | ✓ | ✓ | ✓ |
| All object types (views, procs, indexes, triggers, computed cols) | basic tables/cols | ✓ | ✓ | ✓ |
| Interactive ER diagram | — | ✓ | ✓ | ✓ |
| Schema change detection | — | ✓ | ✓ | ✓ |
| Description cache & concurrency | — | ✓ | ✓ | ✓ |
| **PII scanner + regulation mapping** | — | — | ✓ | ✓ |
| **Compliance reports (HIPAA/GDPR/PCI-DSS)** | — | — | ✓ | ✓ |
| **AI data sampling (`--sample`)** | — | — | ✓ | ✓ |
| Air-gapped deployment, SSO, audit logs, SLA | — | — | — | ✓ |

---

## Free — $0

**Included:** core schema extraction, a single self-contained **HTML** document,
basic tables & columns, and **local** AI descriptions via Ollama (nothing leaves
the network). No PDF/Markdown, no ER diagram, no change detection, no cloud AI.

**Why free.** This is the top of the funnel and our credibility builder. SchemaSpy
and hand-rolled scripts already give teams *something* for free; we have to be at
least as generous to earn a place in the toolchain. A genuinely useful free tier:

- gets sqldoc installed in real environments (the hardest step),
- proves the output quality with the buyer's own schema, and
- creates the "this is nice, but I need PDF / the ER diagram / change tracking"
  moment that drives the upgrade.

Free costs us little: it runs entirely on the user's machine and their own local
LLM — no cloud inference on our dime.

**Competitive frame.** SchemaSpy (free/open source) and Redgate's trial. We match
"free forever" on the basics and win on output polish and the local-first privacy
posture.

---

## Professional — $49 / server / month

**Adds:** full multi-format export (**HTML + PDF + Markdown**), **all object
types** (views & stored procedures with definitions, indexes, triggers, computed
columns), the **interactive ER diagram**, **schema change detection** (git-style
diffs between runs), cloud AI descriptions, and the performance features
(concurrent enrichment + description cache).

**Value case.** This is the full "SQL Doc replacement, but better." The ER
diagram, change detection, and Markdown-for-wikis are the features a data team
actively reaches for every sprint. Schema change detection alone often justifies
the price: catching an unplanned column drop before it breaks a downstream report
is worth far more than $49/mo to the team that owns that report.

**Why $49.** Documentation competitors price around **$220–$500 per user** as
perpetual licenses (dbForge Documenter ≈ $220; Redgate SQL Doc historically
≈ $369–500/user; ApexSQL/Dataedo in a similar band), or **$59–99+ per user per
month** for the subscription/catalog tools. At $49 **per server**, a team with a
handful of engineers pays less than a single competitor seat while covering the
whole instance — and we bill recurring. It's low enough to land on a team credit
card without procurement, and high enough to signal "real product, not a script."

**Target buyer.** Data engineering / platform teams and DBAs who want living,
shareable documentation and drift alerts without a heavyweight catalog.

---

## Compliance — $149 / server / month

**Adds everything in Professional, plus** the **`sqldoc scan`** PII engine:
automated detection of personal/regulated columns, **HIGH/MEDIUM/LOW** risk
ratings, mapping to **HIPAA / GDPR / PCI-DSS**, recommended remediation actions,
the self-contained **compliance report** (risk dashboard + CSV export), and
optional **AI data sampling** (`--sample`) to confirm findings.

**Value case.** This tier is not priced against documentation tools — it's priced
against the *consequences of not doing it* and against the *data-classification
category*. A single mishandled column of card data (PCI-DSS) or health data
(HIPAA) can mean five- to seven-figure fines, breach-notification costs, and
audit findings. A tool that continuously surfaces "here is every column that
looks like regulated data, mapped to the regulation, with a fix" is risk
reduction, not documentation — and risk reduction commands a premium.

**Why $149 (≈3× Professional).** Dedicated data-discovery/classification
platforms (BigID, Immuta, Microsoft Purview, Satori, Dataedo's classification
add-on) are typically **enterprise-priced — often tens of thousands of dollars a
year**, with per-source or per-scan metering. Even mid-market catalog tools treat
sensitive-data classification as a **premium upsell** on top of their base
subscription. At **$149/server/month (~$1,800/year)**, sqldoc's compliance tier
is an order of magnitude cheaper than those platforms while delivering the 80% of
value most SMB/mid-market teams actually need: "find the regulated data and tell
me what to do." The 3× step from Professional is justified because the buyer and
the budget change — this comes out of a security/compliance line item, not a
tooling one, and those buyers measure value against fines and audit cost, not
against a $49 documentation tool.

**Target buyer.** Security, GRC, and compliance owners; regulated industries
(healthcare, fintech, e-commerce) preparing for SOC 2 / HIPAA / PCI / GDPR audits.

**Guardrail that supports the price.** The privacy design — local-first,
row-data-never-read by default, `--sample` opt-in with samples never stored — is
itself a selling point to this buyer. It lets a compliance team run the scanner
against production without creating a *new* data-exposure problem.

---

## Enterprise — custom pricing

**Adds everything in Compliance, plus the controls large/regulated orgs require:**

- **Air-gapped / on-prem deployment** — fully offline operation (local AI only),
  no outbound connectivity, suitable for regulated and classified environments.
- **SSO / SAML / SCIM** — integration with corporate identity; centralized access.
- **Audit logs** — who scanned/what/when, exportable for auditors.
- **SLA & dedicated support** — response-time guarantees, a named contact,
  onboarding and custom-pattern authoring (org-specific PII categories).
- **Volume licensing** — fleet-wide coverage across many servers with a single
  agreement, plus procurement/legal (MSA, DPA, security review) support.

**Why custom.** At org scale the buyer is procurement + security, the deal is
annual and multi-server, and the requirements (air-gap, SSO, audit, MSA/DPA) are
bespoke. Custom pricing lets us capture value proportional to fleet size and risk
surface, and to price in the support/assurance overhead these deals carry.
Anchoring: **land-and-expand** from a few Compliance-tier servers; the reference
point for "custom" is "$149/server × your fleet, discounted for volume and
committed term, plus the enterprise controls."

---

## Competitive landscape (approximate)

| Product | Category | Rough pricing | How sqldoc compares |
|---|---|---|---|
| SchemaSpy | Free docs | $0 (OSS) | We match free basics; win on polish, formats, privacy |
| dbForge Documenter | DB docs | ≈ $220 / user (perpetual) | Pro is per-server subscription; better ER + change detection |
| Redgate SQL Doc | DB docs | ≈ $369–500 / user (perpetual) | Cheaper per team; interactive HTML; drift detection |
| Dataedo | Catalog + classification | ≈ $59–99+ / user / mo; classification is premium | We bundle change detection + PII at a lower, per-server price |
| Purview / BigID / Immuta / Satori | Data discovery & governance | Enterprise (often $10k–$100k+/yr) | Compliance tier delivers core PII value at ~$1.8k/server/yr |

## Why the ladder works

- **Each step changes the buyer.** Free → individual/team trial; Professional →
  data/platform team budget; Compliance → security/GRC budget; Enterprise →
  procurement. Prices rise where budgets and value expand, not linearly with
  features.
- **Each upgrade has a single, obvious trigger.** "I need PDF / the ER diagram /
  drift alerts" → Professional. "We have an audit / we handle card/health data" →
  Compliance. "We're air-gapped / need SSO + a DPA" → Enterprise.
- **Anchoring is deliberate.** Documentation tiers look cheap next to
  $300–500/seat incumbents; the Compliance tier looks cheap next to
  five-figure classification platforms. Both comparisons favor us.

## Open questions to validate

- Per-server vs. per-database metering for very large instances (many DBs/server).
- Whether cloud AI usage should be metered/passed through or absorbed at higher tiers.
- Free-tier limits (e.g. object/table cap) if abuse or cost becomes a factor.
- Regional pricing and non-profit/education discounts.
