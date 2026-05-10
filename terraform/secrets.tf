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
