# terraform-provider-sqldoc (stub)

A **documentation-as-code** integration pattern: run `sqldoc` as part of your
Terraform apply so that every database you provision is also *documented* and
*compliance-scanned* in the same workflow — and the resulting reports become a
tracked, versioned artifact alongside your infrastructure.

> **Status: stub / reference implementation.** This directory documents the
> integration pattern and ships a minimal provider skeleton. It is not yet
> published to the Terraform Registry. The `null_resource` + `local-exec`
> pattern below (Option A) works **today** with zero extra plugins and is the
> recommended way to wire `sqldoc` into IaC right now.

## Why document databases as infrastructure

Schemas drift. A table added by a migration, a new PII column, a permission
grant — these are infrastructure changes as real as a new VM, but they usually
escape the IaC audit trail. Wiring `sqldoc` into Terraform means:

- **Every `terraform apply` regenerates the docs** for the database it manages,
  so the documentation can never fall behind the deployed schema.
- **A `sqldoc scan --fail-on high` gate can block an apply** that would ship a
  regulated-data column without review.
- **Reports are committed as build artifacts** — reviewable in the same PR that
  changes the infrastructure.

## Option A — `null_resource` + `local-exec` (works today)

No custom provider required. Drop this into your Terraform config:

```hcl
resource "null_resource" "sqldoc" {
  # Re-run whenever the database resource changes.
  triggers = {
    db_id = azurerm_mssql_database.app.id
  }

  provisioner "local-exec" {
    command = <<-EOT
      sqldoc doc \
        --connection-string "${var.sqldoc_connection_string}" \
        --output docs/${azurerm_mssql_database.app.name}.html
      sqldoc scan \
        --connection-string "${var.sqldoc_connection_string}" \
        --fail-on high \
        --output docs/${azurerm_mssql_database.app.name}-pii.html
    EOT
  }
}
```

The `scan --fail-on high` call exits non-zero on a HIGH-risk PII finding, which
fails the `terraform apply` — turning compliance into a deploy-time gate.

## Option B — native Terraform provider (this stub)

A native provider would expose data sources so documentation is a first-class
part of the plan/apply lifecycle and its outputs feed other resources:

```hcl
terraform {
  required_providers {
    sqldoc = {
      source  = "htamber1/sqldoc"
      version = "~> 0.1"
    }
  }
}

provider "sqldoc" {
  connection_string = var.sqldoc_connection_string
  dialect           = "sqlserver"   # or postgres, mysql, ...
  mode              = "local"       # local (Ollama) or cloud (Anthropic)
}

# Data source: extract + document the live schema.
data "sqldoc_documentation" "app" {
  output = "docs/app.html"
}

# Data source: PII/compliance posture, usable in policy.
data "sqldoc_pii_scan" "app" {
  fail_on = "high"
}

output "high_risk_pii_columns" {
  value = data.sqldoc_pii_scan.app.high_risk_count
}
```

See `main.go` for the (skeleton) provider entry point and `examples/` for a
runnable configuration.

## Files

| File | Purpose |
|------|---------|
| `main.go` | Provider entry-point skeleton (Terraform Plugin Framework shape). |
| `examples/main.tf` | Reference configuration using both options. |
| `README.md` | This document. |

## Roadmap

1. Implement the `sqldoc_documentation` and `sqldoc_pii_scan` data sources over
   the existing `sqldoc` CLI (shelling out) or a thin Go binding.
2. Add a `sqldoc_compliance_report` data source (HIPAA/GDPR/PCI-DSS).
3. Publish to the Terraform Registry under `htamber1/sqldoc`.
