# Secret Manager Configuration
#
# Terraform owns the secret CONTAINERS — their existence, IAM bindings, and
# replication policy. Terraform does NOT manage the secret VALUES (versions).
# Values are populated out-of-band by the operator after the container is
# created, with:
#
#   echo -n "YOUR_VALUE" | gcloud secrets versions add SECRET_NAME \
#     --data-file=- --project=YOUR_PROJECT_ID
#
# This keeps live secret values out of terraform state, lets operators rotate
# values without re-running terraform, and means a routine `terraform plan`
# never has to ask for or compare against the current secret contents.
#
# For fresh installs, scripts/install.sh handles the post-apply population
# step automatically. For ongoing rotations, just use `gcloud secrets versions
# add` directly — Cloud Run binds env vars to `:latest` so the new version
# takes effect on the next Cloud Run revision (next deploy).

# -----------------------------------------------------------------------------
# Slack signing secret container (created only when var.use_slack is true)
# -----------------------------------------------------------------------------
resource "google_secret_manager_secret" "slack_signing_secret" {
  count     = var.use_slack ? 1 : 0
  secret_id = "slack-signing-secret"

  replication {
    auto {}
  }

  depends_on = [
    google_project_service.secretmanager
  ]
}

# Agent-specific credentials (like Google Chat service account keys) should be created
# manually and granted access to the middleware's Cloud Run service account.
# See docs/FOR_AGENT_DEVELOPERS.md for instructions.

# -----------------------------------------------------------------------------
# Discord bot token container (created only when var.use_discord is true)
#
# Holds the Discord bot token from the Developer Portal. The discord-worker
# VM's service account is granted secretAccessor on this secret in
# terraform/discord_worker.tf; the Forum's Cloud Run service does NOT read
# this secret (the worker is the only thing that talks to Discord directly).
# -----------------------------------------------------------------------------
resource "google_secret_manager_secret" "discord_bot_token" {
  count     = var.use_discord ? 1 : 0
  secret_id = "discord-bot-token"

  replication {
    auto {}
  }

  depends_on = [
    google_project_service.secretmanager
  ]
}

# -----------------------------------------------------------------------------
# Admin UI secret containers (created only when enable_admin_ui is true)
# -----------------------------------------------------------------------------

resource "google_secret_manager_secret" "oauth_client_id" {
  count     = var.enable_admin_ui ? 1 : 0
  secret_id = "oauth-client-id"
  replication {
    auto {}
  }
  depends_on = [google_project_service.secretmanager]
}

resource "google_secret_manager_secret" "oauth_client_secret" {
  count     = var.enable_admin_ui ? 1 : 0
  secret_id = "oauth-client-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.secretmanager]
}

resource "google_secret_manager_secret" "admin_session_secret" {
  count     = var.enable_admin_ui ? 1 : 0
  secret_id = "admin-session-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.secretmanager]
}
