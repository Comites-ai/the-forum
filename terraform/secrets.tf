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

# Note: Discord bot tokens are NOT managed here. Each Discord-enabled
# agent creates its OWN `discord-bot-token` secret in its OWN GCP project
# via the agent-project terraform template, and grants the Forum's
# `discord-worker` service account cross-project secretAccessor. The
# multi-tenant worker reads each agent's token at runtime via Secret
# Manager using the project_id + secret_name carried on the agent's
# Firestore document.

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
