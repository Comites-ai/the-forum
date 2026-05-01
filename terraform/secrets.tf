# Secret Manager Configuration

# Slack Signing Secret
resource "google_secret_manager_secret" "slack_signing_secret" {
  secret_id = "slack-signing-secret"

  replication {
    auto {}
  }

  depends_on = [
    google_project_service.secretmanager
  ]
}

# Note: Secret values must be added manually using:
# echo -n "YOUR_SECRET_VALUE" | gcloud secrets versions add slack-signing-secret --data-file=- --project=YOUR_PROJECT_ID

# Agent-specific credentials (like Google Chat service account keys) should be created
# manually and granted access to the middleware's Cloud Run service account.
# See docs/FOR_AGENT_DEVELOPERS.md for instructions.
