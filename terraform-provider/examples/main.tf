# Reference configuration: wire sqldoc into a Terraform workflow so every
# database you provision is documented + PII-scanned as part of `apply`.

variable "sqldoc_connection_string" {
  type      = string
  sensitive = true
}

# --- Option A: works today, no custom provider ------------------------------
# Regenerate docs + gate on HIGH-risk PII whenever the database changes.

resource "null_resource" "sqldoc_docs" {
  triggers = {
    # Wire this to whatever database resource you manage, e.g.
    # db_id = azurerm_mssql_database.app.id
    always = timestamp()
  }

  provisioner "local-exec" {
    command = <<-EOT
      sqldoc doc  --connection-string "${var.sqldoc_connection_string}" --output docs/app.html
      sqldoc scan --connection-string "${var.sqldoc_connection_string}" --fail-on high --output docs/app-pii.html
    EOT
  }
}

# --- Option B: native provider (stub — not yet published) -------------------
# terraform {
#   required_providers {
#     sqldoc = {
#       source  = "htamber1/sqldoc"
#       version = "~> 0.1"
#     }
#   }
# }
#
# provider "sqldoc" {
#   connection_string = var.sqldoc_connection_string
#   dialect           = "sqlserver"
#   mode              = "local"
# }
#
# data "sqldoc_pii_scan" "app" {
#   fail_on = "high"
# }
#
# output "high_risk_pii_columns" {
#   value = data.sqldoc_pii_scan.app.high_risk_count
# }
