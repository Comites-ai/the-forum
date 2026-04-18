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

# Global MCP endpoint API key
# Used to authenticate Claude Code / owner tools against /api/v1/mcp
resource "google_secret_manager_secret" "mcp_global_api_key" {
  secret_id = "mcp-global-api-key"

  replication {
    auto {}
  }

  depends_on = [
    google_project_service.secretmanager
  ]
}

# Note: Populate the secret after terraform apply:
# openssl rand -base64 32 | tr -d '\n' | \
#   gcloud secrets versions add mcp-global-api-key --data-file=- --project=YOUR_PROJECT_ID
#
# Then set MCP_GLOBAL_API_KEY_SECRET=mcp-global-api-key in the Cloud Run service env vars.
