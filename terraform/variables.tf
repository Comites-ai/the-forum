# Terraform Variables for The Forum Infrastructure

variable "project_id" {
  description = "GCP Project ID (e.g., 'my-workspace-project-123')"
  type        = string
}

variable "region" {
  description = "GCP region for resources"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment name (e.g., 'prod', 'dev')"
  type        = string
  default     = "prod"
}

variable "gcs_bucket_lifecycle_days" {
  description = "Number of days before GCS objects are auto-deleted"
  type        = number
  default     = 1
}

variable "cloud_run_service_name" {
  description = "Name of the Cloud Run service"
  type        = string
  default     = "the-forum"
}

variable "scheduler_job_name" {
  description = "Name of the Cloud Scheduler job"
  type        = string
  default     = "scheduled-jobs-dispatcher"
}

variable "scheduler_cron_schedule" {
  description = "Cron schedule for the scheduler job"
  type        = string
  default     = "* * * * *"
}

variable "use_slack" {
  description = "Whether Slack is in use. When false, terraform does not create the slack-signing-secret container or its IAM binding, and Cloud Run does not get the SLACK_SIGNING_SECRET env binding."
  type        = bool
  default     = true
}

variable "slack_signing_secret_value" {
  description = "Value to populate slack-signing-secret with (one Slack signing secret, or comma-separated list for multiple Slack apps). Required when use_slack is true. Pass via TF_VAR_slack_signing_secret_value rather than terraform.tfvars to keep it out of disk plaintext. Stored in terraform state."
  type        = string
  sensitive   = true
  default     = ""
}

# -----------------------------------------------------------------------------
# Admin UI
# -----------------------------------------------------------------------------

variable "enable_admin_ui" {
  description = "Whether to provision the Google-OAuth-gated admin UI at /admin. When true, terraform creates three Secret Manager secrets (OAuth client id/secret, session secret) and binds them onto the Cloud Run service. When false, the admin UI is not mounted and /admin/* returns 404."
  type        = bool
  default     = false
}

variable "oauth_client_id_value" {
  description = "OAuth 2.0 Web Client ID for the admin UI. Create one in GCP Console → APIs & Services → Credentials. Required when enable_admin_ui is true. Pass via TF_VAR_oauth_client_id_value."
  type        = string
  sensitive   = true
  default     = ""
}

variable "oauth_client_secret_value" {
  description = "OAuth 2.0 Web Client secret for the admin UI. Required when enable_admin_ui is true. Pass via TF_VAR_oauth_client_secret_value."
  type        = string
  sensitive   = true
  default     = ""
}

variable "admin_session_secret_value" {
  description = "Random secret used to sign the admin session cookie. Required when enable_admin_ui is true. Generate with `openssl rand -hex 32` and pass via TF_VAR_admin_session_secret_value."
  type        = string
  sensitive   = true
  default     = ""
}

variable "admin_required_role" {
  description = "IAM role the signed-in operator must hold on project_id to access the admin UI. Defaults to roles/owner. Inherited (folder/org) bindings are intentionally not honored — only direct project bindings."
  type        = string
  default     = "roles/owner"
}
