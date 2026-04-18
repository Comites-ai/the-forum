# Agent Infrastructure - Terraform Template
#
# This template creates dedicated infrastructure for agents.
#
# STRUCTURE:
# Section 1: Common infrastructure (all agents)
# Section 2: Slack-specific infrastructure (uncomment if using Slack)
# Section 3: Google Chat-specific infrastructure (uncomment if using Google Chat)
#
# INSTRUCTIONS:
# 1. Copy this entire directory to your agent repository
# 2. Update terraform.tfvars with your specific values
# 3. Uncomment the sections you need (Slack and/or Google Chat)
# 4. Run: terraform init && terraform apply
# 5. Follow the "next_steps" output for completing configuration

terraform {
  required_version = ">= 1.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  billing_project = var.project_id
  region          = var.region
}

# ==============================================================================
# SECTION 1: COMMON INFRASTRUCTURE (Required for all agents)
# ==============================================================================

# Create the GCP Project
resource "google_project" "agent_project" {
  name            = var.project_name
  project_id      = var.project_id
  org_id          = var.organization_id
  billing_account = var.billing_account

  # Prevent accidental deletion
  lifecycle {
    prevent_destroy = false  # Set to true in production
  }
}

# Use the created project for subsequent resources
provider "google" {
  alias   = "agent"
  project = google_project.agent_project.project_id
  region  = var.region
}

# Enable Secret Manager API (used by all platforms)
resource "google_project_service" "secretmanager" {
  project = google_project.agent_project.project_id
  service = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

# Enable Google Drive API (most agents use Google Drive)
resource "google_project_service" "drive" {
  project = google_project.agent_project.project_id
  service = "drive.googleapis.com"
  disable_on_destroy = false
}

# Enable Google Sheets API (most agents use Google Sheets)
resource "google_project_service" "sheets" {
  project = google_project.agent_project.project_id
  service = "sheets.googleapis.com"
  disable_on_destroy = false
}

# Enable Google Docs API (for agents that use Google Docs for memory/notes)
resource "google_project_service" "docs" {
  project = google_project.agent_project.project_id
  service = "docs.googleapis.com"
  disable_on_destroy = false
}

# Service Account for Google APIs (Drive, Sheets, Docs, etc.)
# This SA will be used by your agent to access Google Drive and Sheets
resource "google_service_account" "agent_apis" {
  project      = google_project.agent_project.project_id
  account_id   = "${var.bot_account_id}-apis"
  display_name = "${var.bot_name} Google APIs"
  description  = "Service account for ${var.bot_name} to access Google APIs (Drive, Sheets, etc.)"

  depends_on = [
    google_project_service.drive,
    google_project_service.sheets
  ]
}

# Allow service account key creation for this project
# This overrides the organization policy that blocks key creation
resource "google_project_organization_policy" "allow_sa_key_creation" {
  project    = google_project.agent_project.project_id
  constraint = "constraints/iam.disableServiceAccountKeyCreation"

  boolean_policy {
    enforced = false
  }

  depends_on = [
    google_project.agent_project
  ]
}

# Enable Cloud Storage API (for staging bucket)
resource "google_project_service" "storage" {
  project = google_project.agent_project.project_id
  service = "storage.googleapis.com"
  disable_on_destroy = false
}

# Staging bucket for ADK deployments
# ADK uses this bucket to upload agent code before deploying to Vertex AI
resource "google_storage_bucket" "staging" {
  project       = google_project.agent_project.project_id
  name          = "${var.project_id}-staging"
  location      = var.region
  force_destroy = false  # Protect against accidental deletion

  uniform_bucket_level_access = true

  # Auto-delete old staging files after 7 days
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [
    google_project_service.storage
  ]
}

# ==============================================================================
# SECTION 2: SLACK-SPECIFIC INFRASTRUCTURE
# Uncomment this section if your agent uses Slack
# ==============================================================================

# # Slack Bot Token Secret
# resource "google_secret_manager_secret" "slack_bot_token" {
#   project   = google_project.agent_project.project_id
#   secret_id = "${var.bot_account_id}-slack-token"
#
#   replication {
#     auto {}
#   }
#
#   depends_on = [
#     google_project_service.secretmanager
#   ]
# }

# Note: The Slack bot token must be added manually after terraform apply:
# echo -n "xoxb-YOUR-SLACK-BOT-TOKEN" | gcloud secrets versions add ${var.bot_account_id}-slack-token \
#   --data-file=- --project=${var.project_id}

# ==============================================================================
# SECTION 3: GOOGLE CHAT-SPECIFIC INFRASTRUCTURE
# Uncomment this section if your agent uses Google Chat
# ==============================================================================

# # Enable Google Chat API
# resource "google_project_service" "chat" {
#   project = google_project.agent_project.project_id
#   service = "chat.googleapis.com"
#   disable_on_destroy = false
# }

# # Service Account for Google Chat bot
# # This SA will be used for Google Chat API calls (sending messages)
# resource "google_service_account" "chat_bot" {
#   project      = google_project.agent_project.project_id
#   account_id   = var.bot_account_id
#   display_name = var.bot_name
#   description  = "Service account for ${var.bot_name} Google Chat bot"
#
#   depends_on = [
#     google_project_service.chat
#   ]
# }

# # Grant Google Chat bot permissions
# resource "google_project_iam_member" "chat_owner" {
#   project = google_project.agent_project.project_id
#   role    = "roles/chat.owner"
#   member  = "serviceAccount:${google_service_account.chat_bot.email}"
# }

# # Store Google Chat service account credentials in Secret Manager
# resource "google_secret_manager_secret" "chat_credentials" {
#   project   = google_project.agent_project.project_id
#   secret_id = var.secret_name
#
#   replication {
#     auto {}
#   }
#
#   depends_on = [
#     google_project_service.secretmanager
#   ]
# }

# ==============================================================================
# SECTION 4: TELEGRAM-SPECIFIC INFRASTRUCTURE
# Uncomment this section if your agent uses Telegram
# ==============================================================================

# # Telegram Bot Token Secret
# resource "google_secret_manager_secret" "telegram_bot_token" {
#   project   = google_project.agent_project.project_id
#   secret_id = "${var.bot_account_id}-telegram-token"
#
#   replication {
#     auto {}
#   }
#
#   depends_on = [
#     google_project_service.secretmanager
#   ]
# }

# Note: The Telegram bot token must be added manually after terraform apply:
# echo -n "YOUR_TELEGRAM_BOT_TOKEN" | gcloud secrets versions add ${var.bot_account_id}-telegram-token \
#   --data-file=- --project=${var.project_id}

# ==============================================================================
# SECTION 5: CUSTOM MCP SERVER (Uncomment if deploying your own MCP server)
# This section creates a Cloud Run service for a custom MCP server and stores
# its API key so the middleware can authenticate when proxying tool calls.
# See docs/USING_MCP_SERVER.md for a guide on building the server itself.
# ==============================================================================

# # Enable Cloud Run API
# resource "google_project_service" "run" {
#   project            = google_project.agent_project.project_id
#   service            = "run.googleapis.com"
#   disable_on_destroy = false
# }
#
# # Enable Artifact Registry (to store your MCP server image)
# resource "google_project_service" "artifactregistry" {
#   project            = google_project.agent_project.project_id
#   service            = "artifactregistry.googleapis.com"
#   disable_on_destroy = false
# }
#
# # Cloud Run service hosting the custom MCP server
# # Build and push your image first:
# #   docker build -t gcr.io/${var.project_id}/${var.bot_account_id}-mcp:latest .
# #   docker push gcr.io/${var.project_id}/${var.bot_account_id}-mcp:latest
# resource "google_cloud_run_v2_service" "mcp_server" {
#   name     = "${var.bot_account_id}-mcp"
#   location = var.region
#   project  = google_project.agent_project.project_id
#
#   template {
#     containers {
#       image = "gcr.io/${google_project.agent_project.project_id}/${var.bot_account_id}-mcp:latest"
#
#       env {
#         name  = "PORT"
#         value = "8080"
#       }
#     }
#   }
#
#   depends_on = [
#     google_project_service.run,
#     google_project_service.artifactregistry,
#   ]
# }
#
# # Allow unauthenticated invocations — auth is handled via X-API-Key header
# resource "google_cloud_run_v2_service_iam_member" "mcp_server_public" {
#   project  = google_project.agent_project.project_id
#   location = var.region
#   name     = google_cloud_run_v2_service.mcp_server.name
#   role     = "roles/run.invoker"
#   member   = "allUsers"
# }
#
# # API key secret — the middleware sends this in the X-API-Key header
# resource "google_secret_manager_secret" "mcp_api_key" {
#   project   = google_project.agent_project.project_id
#   secret_id = "${var.bot_account_id}-mcp-api-key"
#
#   replication {
#     auto {}
#   }
#
#   depends_on = [google_project_service.secretmanager]
# }
#
# # Grant the middleware Cloud Run SA access to the MCP API key secret
# resource "google_secret_manager_secret_iam_member" "mcp_api_key_accessor" {
#   project   = google_project.agent_project.project_id
#   secret_id = google_secret_manager_secret.mcp_api_key.secret_id
#   role      = "roles/secretmanager.secretAccessor"
#   member    = "serviceAccount:${data.google_project.middleware.number}-compute@developer.gserviceaccount.com"
# }

# ==============================================================================
# IAM BINDINGS - Grant middleware access to secrets
# ==============================================================================

# Get the middleware project number for service account email
data "google_project" "middleware" {
  project_id = var.middleware_project_id
}

# Grant middleware access to Slack bot token
resource "google_secret_manager_secret_iam_member" "slack_token_accessor" {
  project   = google_project.agent_project.project_id
  secret_id = google_secret_manager_secret.slack_bot_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.middleware.number}-compute@developer.gserviceaccount.com"
}

# Grant middleware access to Google Chat credentials
resource "google_secret_manager_secret_iam_member" "chat_credentials_accessor" {
  project   = google_project.agent_project.project_id
  secret_id = google_secret_manager_secret.chat_credentials.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.middleware.number}-compute@developer.gserviceaccount.com"
}

# Grant middleware access to Telegram bot token
resource "google_secret_manager_secret_iam_member" "telegram_token_accessor" {
  project   = google_project.agent_project.project_id
  secret_id = google_secret_manager_secret.telegram_bot_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.middleware.number}-compute@developer.gserviceaccount.com"
}

# ==============================================================================
# OUTPUTS
# ==============================================================================

output "project_id" {
  description = "GCP Project ID for the agent"
  value       = var.project_id
}

output "apis_service_account_email" {
  description = "Service account email for Google APIs (Drive, Sheets) - share your Google Docs with this"
  value       = google_service_account.agent_apis.email
}

output "staging_bucket" {
  description = "GCS bucket for ADK deployment staging"
  value       = google_storage_bucket.staging.name
}

# Uncomment if using Google Chat
# output "chat_service_account_email" {
#   description = "Service account email for Google Chat bot"
#   value       = google_service_account.chat_bot.email
# }

output "next_steps" {
  description = "Instructions for completing the setup"
  value       = <<EOT

==================== NEXT STEPS ====================

SECTION 1: COMMON SETUP (All agents)

1a. The staging bucket has been created: gs://${google_storage_bucket.staging.name}
    This bucket is used by ADK to upload agent code before deploying to Vertex AI.

1b. Deploy your agent to Vertex AI using ADK:
    cd /path/to/your-agent

    adk deploy agent_engine \
      --project ${var.project_id} \
      --region ${var.region} \
      --staging_bucket gs://${google_storage_bucket.staging.name} \
      --display_name "${var.bot_name}" \
      --trace_to_cloud \
      your-agent-directory

    Note the Reasoning Engine ID from the output:
    projects/${var.project_id}/locations/${var.region}/reasoningEngines/ENGINE_ID

1c. Review what you uncommented in main.tf and proceed with relevant sections below

SECTION 2: SLACK SETUP (If using Slack)

2a. Store the Slack bot token in Secret Manager:
    echo -n "xoxb-YOUR-SLACK-BOT-TOKEN" | gcloud secrets versions add ${var.bot_account_id}-slack-token \
      --data-file=- --project=${var.project_id}

2b. Grant middleware access to the Slack token:
    export MIDDLEWARE_PROJECT_ID="YOUR_MIDDLEWARE_PROJECT"
    export MIDDLEWARE_PROJECT_NUMBER=$(gcloud projects describe $MIDDLEWARE_PROJECT_ID --format="value(projectNumber)")
    export MIDDLEWARE_SA="$${MIDDLEWARE_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

    gcloud secrets add-iam-policy-binding ${var.bot_account_id}-slack-token \
      --member="serviceAccount:$${MIDDLEWARE_SA}" \
      --role="roles/secretmanager.secretAccessor" \
      --project=${var.project_id}

    CRITICAL: Without this IAM binding, Slack messages will fail with "403 Permission Denied" errors!

2c. Register your agent with middleware using the Slack platform configuration

SECTION 3: GOOGLE CHAT SETUP (If using Google Chat)

3a. Create a service account key:
    gcloud iam service-accounts keys create ${var.bot_account_id}-sa-key.json \
      --iam-account=SERVICE_ACCOUNT_EMAIL_FROM_OUTPUT \
      --project=${var.project_id}

3b. Store the key in YOUR AGENT'S project Secret Manager (NOT middleware):
    gcloud secrets versions add ${var.secret_name} \
      --data-file=${var.bot_account_id}-sa-key.json \
      --project=${var.project_id}

    # Securely delete the key file
    rm -f ${var.bot_account_id}-sa-key.json

3c. Grant middleware access to the Google Chat credentials:
    export MIDDLEWARE_PROJECT_ID="YOUR_MIDDLEWARE_PROJECT"
    export MIDDLEWARE_PROJECT_NUMBER=$(gcloud projects describe $MIDDLEWARE_PROJECT_ID --format="value(projectNumber)")
    export MIDDLEWARE_SA="$${MIDDLEWARE_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

    gcloud secrets add-iam-policy-binding ${var.secret_name} \
      --member="serviceAccount:$${MIDDLEWARE_SA}" \
      --role="roles/secretmanager.secretAccessor" \
      --project=${var.project_id}

    CRITICAL: Without this IAM binding, Google Chat messages will fail with "403 Permission Denied" errors!

3d. Configure Google Chat bot in Console:
    - Go to: https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${var.project_id}
    - Click "Configuration"
    - Bot name: ${var.bot_name}
    - Avatar URL: ${var.bot_avatar_url}
    - Description: ${var.bot_description}
    - Functionality: "Receive 1:1 messages" and "Join spaces and group conversations"
    - Connection settings: "App URL"
    - Bot URL: YOUR_MIDDLEWARE_URL/api/v1/google-chat/events
    - Permissions: "Specific people and groups" (add test users)

3e. Enable Google Chat for your agent in middleware:
    python scripts/enable_google_chat_agent.py \
      --project $MIDDLEWARE_PROJECT_ID \
      --agent-id YOUR_AGENT_ID \
      --secret-name ${var.secret_name} \
      --google-chat-project-id ${var.project_id}

SECTION 4: TELEGRAM SETUP (If using Telegram)

4a. Create Telegram bot via BotFather:
    - Open Telegram and message @BotFather
    - Send command: /newbot
    - Follow prompts to choose name and username
    - Copy the bot token (format: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz)

4b. Store the Telegram bot token in Secret Manager:
    echo -n "YOUR_TELEGRAM_BOT_TOKEN" | gcloud secrets versions add ${var.bot_account_id}-telegram-token \
      --data-file=- --project=${var.project_id}

4c. Grant middleware access to the Telegram token:
    export MIDDLEWARE_PROJECT_ID="YOUR_MIDDLEWARE_PROJECT"
    export MIDDLEWARE_PROJECT_NUMBER=$(gcloud projects describe $MIDDLEWARE_PROJECT_ID --format="value(projectNumber)")
    export MIDDLEWARE_SA="$${MIDDLEWARE_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

    gcloud secrets add-iam-policy-binding ${var.bot_account_id}-telegram-token \
      --member="serviceAccount:$${MIDDLEWARE_SA}" \
      --role="roles/secretmanager.secretAccessor" \
      --project=${var.project_id}

    CRITICAL: Without this IAM binding, Telegram messages will fail with "403 Permission Denied" errors!

4d. Set Telegram webhook:
    # Generate a random secret token for webhook verification
    export WEBHOOK_SECRET=$(openssl rand -base64 32)

    # Set the webhook
    curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
      -H "Content-Type: application/json" \
      -d '{
        "url": "https://YOUR_MIDDLEWARE_URL/api/v1/telegram/events",
        "secret_token": "'$WEBHOOK_SECRET'"
      }'

    # Save the webhook secret for agent configuration
    echo "Webhook secret: $WEBHOOK_SECRET"

4e. Enable Telegram for your agent in middleware using Firestore console or script

SECTION 5: CUSTOM MCP SERVER SETUP (If using a custom MCP server)

5a. Build and push your MCP server image:
    cd /path/to/your-mcp-server
    docker build -t gcr.io/${var.project_id}/${var.bot_account_id}-mcp:latest .
    docker push gcr.io/${var.project_id}/${var.bot_account_id}-mcp:latest

5b. Uncomment SECTION 5 in main.tf, then apply:
    terraform apply

5c. Generate and store the MCP API key:
    openssl rand -base64 32 | tr -d '\n' | \
      gcloud secrets versions add ${var.bot_account_id}-mcp-api-key \
        --data-file=- --project=${var.project_id}

5d. Register the MCP server in your agent's Firestore document.
    Add the following to the agent's mcp_servers array in the Firebase console
    (Firestore → agents → YOUR_AGENT_ID):
    {
      "name": "${var.bot_account_id}",
      "url":  "https://MCP_SERVER_CLOUD_RUN_URL/sse",
      "enabled": true,
      "api_key_secret": "${var.bot_account_id}-mcp-api-key",
      "api_key_project_id": "${var.project_id}",
      "api_key_header": "X-API-Key"
    }

5e. Configure your ADK agent to use the middleware MCP endpoint:
    In your agent code, add MCPToolset pointing to the middleware:
      MCPToolset(
        connection_params=SseServerParams(
          url="MIDDLEWARE_URL/api/v1/mcp/AGENT_FIRESTORE_ID/sse"
        )
      )
    See docs/FOR_AGENT_DEVELOPERS.md → "Using MCP Servers with Your Agent"

SECTION 6: GOOGLE APIS SETUP (Share Google Drive/Sheets)

6a. Share Google Sheets/Drive files with the Google APIs service account:
    Service Account Email: ${google_service_account.agent_apis.email}

    Instructions:
    - Open your Google Sheet or Drive file
    - Click "Share"
    - Add the service account email above
    - Give it "Editor" or "Viewer" access (depending on agent needs)

====================================================

EOT
}
