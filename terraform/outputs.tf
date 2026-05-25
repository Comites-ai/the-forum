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
  value       = google_cloud_run_v2_service.forum.uri
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
  value       = "${google_cloud_run_v2_service.forum.uri}/api/v1/slack/events"
}

output "google_chat_webhook_url" {
  description = "Google Chat webhook URL (use in Google Chat bot configuration)"
  value       = "${google_cloud_run_v2_service.forum.uri}/api/v1/google-chat/events"
}

output "discord_worker_service_account" {
  description = "Email of the discord-worker VM service account. Every Discord-enabled agent puts this exact email in its Firestore document's `discord_worker_service_account` field so the Forum will accept events forwarded by this worker. Same email for every agent — the worker is multi-tenant. Only meaningful when use_discord is true."
  # try() guards the [0] index against count=0; never errors when use_discord=false.
  value = try(google_service_account.discord_worker[0].email, "")
}

output "discord_worker_vm_name" {
  description = "Compute Engine VM name for the discord-worker (when use_discord is true)."
  value       = try(google_compute_instance.discord_worker[0].name, "")
}

locals {
  # When use_discord=false this local evaluates to "". The format() call is
  # only reached when use_discord=true, but we still wrap the [0] reads in
  # try() as belt-and-braces against terraform's eager evaluation rules.
  discord_setup_instructions_text = var.use_discord ? format(
    <<-EOT

      ==================== DISCORD SETUP ====================

      A single multi-tenant worker has been provisioned. It will discover
      Discord-enabled agents from the Firestore agents collection at
      runtime (refresh every 5 min by default) and open one Gateway
      connection per agent in a single Python process.

      Worker VM:
        Name:    %s
        Zone:    %s
        Machine: %s

      Cost reminder:
        An e2-micro VM in us-central1, us-west1, or us-east1 is included
        in the GCP Always Free tier — ONE VM per billing account. If that
        allowance is already consumed (or you picked a different region),
        this VM will be billed at the standard rate (~$6-7/month).

      Patching reminder:
        The VM auto-patches its host OS (Container-Optimized OS). The
        worker container image is pinned — rebuild it quarterly or in
        response to CVEs:
          gcloud builds submit discord-worker \
            --tag=%s \
            --project=%s
        then either reboot the VM or wait for the next maintenance event.

      To add a new Discord agent (per agent, in the AGENT'S project):
        1. Use the Agent-Template repo (github.com/Comites-ai/Agent-Template)
           — uncomment SECTION 5 of its terraform/main.tf and apply, which
           creates:
             - discord-bot-token secret container (in agent's project)
             - cross-project secretAccessor grant to:
               %s
        2. Populate the bot token in the agent's project:
             echo -n "YOUR_BOT_TOKEN" | gcloud secrets versions add \
               discord-bot-token --data-file=- --project=<agent-project>
        3. Add a discord platform block to the agent's Firestore doc:
             {
               "platform": "discord",
               "enabled": true,
               "discord_bot_token_secret": "discord-bot-token",
               "discord_bot_token_project_id": "<agent-project>",
               "discord_worker_service_account": "%s"
             }
        4. Wait up to AGENT_REFRESH_INTERVAL_SECONDS (default 300s) for
           the worker to pick up the new bot. Or `gcloud compute instances
           reset discord-worker` to force an immediate reconcile.

      ======================================================
    EOT
    ,
    try(google_compute_instance.discord_worker[0].name, ""),
    var.discord_worker_zone,
    var.discord_worker_machine_type,
    var.discord_worker_image,
    var.project_id,
    try(google_service_account.discord_worker[0].email, ""),
    try(google_service_account.discord_worker[0].email, ""),
  ) : ""
}

output "discord_setup_instructions" {
  description = "Discord-specific setup steps. Only meaningful when use_discord is true."
  value       = local.discord_setup_instructions_text
}

output "admin_redirect_uri" {
  description = "Redirect URI to register on the OAuth client and set as OAUTH_REDIRECT_URI on the Cloud Run service. Only meaningful when enable_admin_ui is true."
  value       = "${google_cloud_run_v2_service.forum.uri}/admin/auth/callback"
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
       ${google_cloud_run_v2_service.forum.uri}/api/v1/slack/events

    3. Google Chat webhook URL (if using Google Chat bots):
       ${google_cloud_run_v2_service.forum.uri}/api/v1/google-chat/events

    4. For agent-specific setup (Google Chat bots, etc.):
       See docs/FOR_AGENT_DEVELOPERS.md for complete instructions

    ${var.enable_admin_ui ? "5. Admin UI bring-up: register this URL as an authorized redirect URI on your OAuth client, then set it on the Cloud Run service:\n         ${google_cloud_run_v2_service.forum.uri}/admin/auth/callback\n       gcloud run services update ${var.cloud_run_service_name} \\\n         --region ${var.region} \\\n         --update-env-vars OAUTH_REDIRECT_URI=${google_cloud_run_v2_service.forum.uri}/admin/auth/callback\n       Then visit ${google_cloud_run_v2_service.forum.uri}/admin/" : ""}

    ====================================================
  EOT
}
