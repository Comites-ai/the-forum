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
# Discord
#
# Discord is unlike Slack/Telegram/Google Chat: it does NOT deliver direct
# messages over HTTP webhooks. DMs only arrive over the Gateway WebSocket
# protocol, which means we need a long-running process to receive them.
# When use_discord is true, terraform provisions an e2-micro Compute Engine
# VM (the "discord-worker") that holds the Gateway connection open and
# forwards each DM to the Forum's /api/v1/discord/events/{agent_id} endpoint.
#
# COST NOTE: an e2-micro is included in GCP's Always Free tier when
# deployed in us-central1, us-west1, or us-east1 — one VM per billing
# account. If your free-tier e2-micro allowance is already in use by another
# VM, or you deploy this in a different region, you will be billed at the
# standard e2-micro rate (~$6-7/month). Verify billing before applying.
#
# OS PATCHING: the VM runs Container-Optimized OS with automatic updates
# enabled, so the host OS patches itself. The worker container image is
# pinned and must be rebuilt and redeployed manually to pick up new
# discord.py or Python security fixes. See docs/DISCORD_WORKER.md for the
# redeploy runbook; review and rebuild quarterly or in response to CVEs.
# -----------------------------------------------------------------------------

variable "use_discord" {
  description = "Whether Discord is in use. When true, terraform creates the discord-bot-token secret, a service account for the worker VM, and an e2-micro Compute Engine VM running the discord-worker container. See docs/DISCORD_WORKER.md."
  type        = bool
  default     = false
}

variable "discord_bot_token_value" {
  description = "Value of the Discord bot token (from Discord Developer Portal → Bot → Reset Token). Required when use_discord is true. Pass via TF_VAR_discord_bot_token_value to keep it out of disk plaintext. Stored in terraform state."
  type        = string
  sensitive   = true
  default     = ""
}

variable "discord_agent_id" {
  description = "Firestore agent document ID this Discord worker forwards events to. Required when use_discord is true. The worker POSTs to /api/v1/discord/events/{discord_agent_id}."
  type        = string
  default     = ""
}

variable "discord_worker_image" {
  description = "Container image for the discord-worker VM. Build with `gcloud builds submit discord-worker --tag=...` and pass the resulting image URL here. Required when use_discord is true."
  type        = string
  default     = ""
}

variable "discord_worker_zone" {
  description = "Compute Engine zone for the discord-worker VM. To stay in the GCP Always Free tier, pick a zone in us-central1, us-west1, or us-east1."
  type        = string
  default     = "us-central1-a"
}

variable "discord_worker_machine_type" {
  description = "Machine type for the discord-worker VM. e2-micro is the cheapest option and is sufficient for Gateway traffic; it is also the type covered by the Always Free tier. Increase only if the worker proves CPU- or memory-bound."
  type        = string
  default     = "e2-micro"
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
