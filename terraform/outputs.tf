# Terraform Outputs

output "project_id" {
  description = "GCP Project ID"
  value       = var.project_id
}

output "region" {
  description = "GCP Region"
  value       = var.region
}

output "cloud_run_url" {
  description = "Cloud Run service URL"
  value       = google_cloud_run_v2_service.middleware.uri
}

output "gcs_bucket_name" {
  description = "GCS bucket name for Slack files"
  value       = google_storage_bucket.slack_files.name
}

output "scheduler_service_account_email" {
  description = "Scheduler service account email"
  value       = google_service_account.scheduler.email
}

output "slack_webhook_url" {
  description = "Slack webhook URL (use in Slack app Event Subscriptions)"
  value       = "${google_cloud_run_v2_service.middleware.uri}/api/v1/slack/events"
}

output "google_chat_webhook_url" {
  description = "Google Chat webhook URL (use in Google Chat bot configuration)"
  value       = "${google_cloud_run_v2_service.middleware.uri}/api/v1/google-chat/events"
}

output "next_steps" {
  description = "Next steps after Terraform apply"
  value       = <<-EOT

    ==================== NEXT STEPS ====================

    1. Add Slack signing secret(s) to Secret Manager:
       echo -n "YOUR_SLACK_SECRET" | gcloud secrets versions add slack-signing-secret \
         --data-file=- --project=${var.project_id}

       If you have multiple Slack apps, add all signing secrets comma-separated.

    2. Update Slack app webhook URL (for each Slack bot):
       ${google_cloud_run_v2_service.middleware.uri}/api/v1/slack/events

    3. Google Chat webhook URL (if using Google Chat bots):
       ${google_cloud_run_v2_service.middleware.uri}/api/v1/google-chat/events

    4. For agent-specific setup (Google Chat bots, etc.):
       See docs/FOR_AGENT_DEVELOPERS.md for complete instructions

    ====================================================
  EOT
}
