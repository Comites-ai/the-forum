# Secret Manager Configuration

# Slack Signing Secret (created only when var.use_slack is true)
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

# Initial value for the secret. Cloud Run validates that bound secrets have
# at least one version when creating a revision, so terraform must populate
# the version in the same apply that creates the Cloud Run service. The
# value is supplied via var.slack_signing_secret_value (typically passed
# through TF_VAR_slack_signing_secret_value by scripts/install.sh).
#
# To rotate post-install: use 'gcloud secrets versions add' or update the
# variable and re-apply. Cloud Run binds to ':latest' so the new version
# takes effect on the next revision.
resource "google_secret_manager_secret_version" "slack_signing_secret" {
  count       = var.use_slack ? 1 : 0
  secret      = google_secret_manager_secret.slack_signing_secret[0].id
  secret_data = var.slack_signing_secret_value

  lifecycle {
    precondition {
      condition     = length(var.slack_signing_secret_value) > 0
      error_message = "When use_slack is true, slack_signing_secret_value must be non-empty. Pass it via TF_VAR_slack_signing_secret_value (scripts/install.sh does this for you)."
    }
  }
}

# Note: Secret values must be added manually using:
# echo -n "YOUR_SECRET_VALUE" | gcloud secrets versions add slack-signing-secret --data-file=- --project=YOUR_PROJECT_ID

# Agent-specific credentials (like Google Chat service account keys) should be created
# manually and granted access to the middleware's Cloud Run service account.
# See docs/FOR_AGENT_DEVELOPERS.md for instructions.

# -----------------------------------------------------------------------------
# Discord bot token (created only when var.use_discord is true)
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

resource "google_secret_manager_secret_version" "discord_bot_token" {
  count       = var.use_discord ? 1 : 0
  secret      = google_secret_manager_secret.discord_bot_token[0].id
  secret_data = var.discord_bot_token_value

  lifecycle {
    precondition {
      condition     = length(var.discord_bot_token_value) > 0
      error_message = "When use_discord is true, discord_bot_token_value must be non-empty. Pass via TF_VAR_discord_bot_token_value."
    }
  }
}

# -----------------------------------------------------------------------------
# Admin UI secrets (created only when enable_admin_ui is true)
# -----------------------------------------------------------------------------

resource "google_secret_manager_secret" "oauth_client_id" {
  count     = var.enable_admin_ui ? 1 : 0
  secret_id = "oauth-client-id"
  replication {
    auto {}
  }
  depends_on = [google_project_service.secretmanager]
}

resource "google_secret_manager_secret_version" "oauth_client_id" {
  count       = var.enable_admin_ui ? 1 : 0
  secret      = google_secret_manager_secret.oauth_client_id[0].id
  secret_data = var.oauth_client_id_value
  lifecycle {
    precondition {
      condition     = length(var.oauth_client_id_value) > 0
      error_message = "When enable_admin_ui is true, oauth_client_id_value must be non-empty. Pass via TF_VAR_oauth_client_id_value."
    }
  }
}

resource "google_secret_manager_secret" "oauth_client_secret" {
  count     = var.enable_admin_ui ? 1 : 0
  secret_id = "oauth-client-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.secretmanager]
}

resource "google_secret_manager_secret_version" "oauth_client_secret" {
  count       = var.enable_admin_ui ? 1 : 0
  secret      = google_secret_manager_secret.oauth_client_secret[0].id
  secret_data = var.oauth_client_secret_value
  lifecycle {
    precondition {
      condition     = length(var.oauth_client_secret_value) > 0
      error_message = "When enable_admin_ui is true, oauth_client_secret_value must be non-empty. Pass via TF_VAR_oauth_client_secret_value."
    }
  }
}

resource "google_secret_manager_secret" "admin_session_secret" {
  count     = var.enable_admin_ui ? 1 : 0
  secret_id = "admin-session-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.secretmanager]
}

resource "google_secret_manager_secret_version" "admin_session_secret" {
  count       = var.enable_admin_ui ? 1 : 0
  secret      = google_secret_manager_secret.admin_session_secret[0].id
  secret_data = var.admin_session_secret_value
  lifecycle {
    precondition {
      condition     = length(var.admin_session_secret_value) > 0
      error_message = "When enable_admin_ui is true, admin_session_secret_value must be non-empty. Generate via `openssl rand -hex 32` and pass via TF_VAR_admin_session_secret_value."
    }
  }
}
