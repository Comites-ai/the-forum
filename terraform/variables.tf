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
