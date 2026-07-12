# sqldoc agent on Google Cloud Run (gen2), with a Cloud Storage bucket mounted at
# /data (gcsfuse) for the agent store and env-var configuration for credentials.
#
#   terraform init
#   terraform apply -var project=my-project -var image=gcr.io/my-project/sqldoc:2.7.0 \
#     -var config_base64="$(base64 -w0 .sqldoc.yml)"

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

variable "project" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "image" { type = string }
variable "name" {
  type    = string
  default = "sqldoc"
}
variable "config_base64" {
  type      = string
  sensitive = true
}
variable "anthropic_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

provider "google" {
  project = var.project
  region  = var.region
}

# Persistent store for the agent (mounted at /data via gcsfuse).
resource "google_storage_bucket" "data" {
  name                        = "${var.name}-data-${var.project}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
}

resource "google_service_account" "sqldoc" {
  account_id   = "${var.name}-run"
  display_name = "sqldoc Cloud Run"
}

resource "google_storage_bucket_iam_member" "data_access" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.sqldoc.email}"
}

resource "google_cloud_run_v2_service" "sqldoc" {
  name     = var.name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.sqldoc.email
    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }

    volumes {
      name = "data"
      gcs {
        bucket    = google_storage_bucket.data.name
        read_only = false
      }
    }

    containers {
      image = var.image
      command = ["/bin/sh", "-c"]
      args    = ["echo \"$CONFIG_B64\" | base64 -d > /app/.sqldoc.yml && exec sqldoc agent start --foreground"]

      ports {
        container_port = 8080
      }

      env {
        name  = "SQLDOC_AGENT_HOME"
        value = "/data"
      }
      env {
        name  = "CONFIG_B64"
        value = var.config_base64
      }
      dynamic "env" {
        for_each = var.anthropic_api_key == "" ? [] : [1]
        content {
          name  = "ANTHROPIC_API_KEY"
          value = var.anthropic_api_key
        }
      }

      volume_mounts {
        name       = "data"
        mount_path = "/data"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }
    }
  }
}

output "url" {
  value = google_cloud_run_v2_service.sqldoc.uri
}
