# Google Cloud Storage Configuration

# Staging bucket for ADK Agent Engine deployments.
# ADK uploads agent_engine.pkl, dependencies.tar.gz, and requirements.txt here
# when deploying an agent to Vertex AI Reasoning Engine in this project.
# Shared across all agents that deploy into this project.
resource "google_storage_bucket" "staging" {
  name     = "${var.project_id}-staging"
  location = var.region

  uniform_bucket_level_access = true

  # Auto-delete staging artifacts after 7 days; redeploys regenerate them.
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 7
    }
  }

  # force_destroy=false: artifacts are recoverable via redeploy, but require
  # an explicit empty step before terraform destroy. Prevents accidental loss.
  force_destroy = false

  depends_on = [
    google_project_service.storage
  ]
}

# GCS Bucket for Slack file uploads
resource "google_storage_bucket" "slack_files" {
  name     = "${var.project_id}-slack-files"
  location = var.region

  # Uniform bucket-level access
  uniform_bucket_level_access = true

  # Lifecycle rule to auto-delete files after specified days
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = var.gcs_bucket_lifecycle_days
    }
  }

  # Enable versioning for safety (optional)
  versioning {
    enabled = false
  }

  # Force destroy to allow Terraform to delete bucket even if not empty
  # Set to false in production if you want to prevent accidental deletion
  force_destroy = true

  depends_on = [
    google_project_service.storage
  ]
}
