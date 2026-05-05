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

# Service Account for the agent.
# A single SA is used for everything: Google APIs (Drive/Sheets/Docs) and,
# when Google Chat is enabled, sending Chat messages. Share your spreadsheets
# and docs with this SA's email; its key is what gets stored in Secret Manager.
resource "google_service_account" "agent" {
  project      = google_project.agent_project.project_id
  account_id   = var.bot_account_id
  display_name = var.bot_name
  description  = "Service account for ${var.bot_name} (Google APIs + platform integrations)"

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

# # Grant the agent SA permission to send Google Chat messages.
# # We reuse google_service_account.agent (created in Section 1) — the same SA
# # is used for both Google APIs (Sheets/Docs) and Google Chat.
# resource "google_project_iam_member" "chat_owner" {
#   project = google_project.agent_project.project_id
#   role    = "roles/chat.owner"
#   member  = "serviceAccount:${google_service_account.agent.email}"
# }

# # Store the agent SA's key in Secret Manager so the middleware can read it
# # to authenticate Google Chat API calls.
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
# SECTION 5: SCHEDULER MCP KEY (Uncomment to use the middleware's scheduler MCP)
# Provisions the secret container that holds your agent's scheduler MCP API
# key, plus an IAM binding so the agent's Reasoning Engine SA can read it at
# runtime to send in the X-API-Key header. You still run the provision script
# (in the middleware repo) to generate the actual key value and add it via
# `gcloud secrets versions add` — secret values shouldn't live in terraform.
# See FOR_AGENT_DEVELOPERS.md §9 for the end-to-end flow.
# ==============================================================================

# # Secret container for the scheduler MCP API key. Empty until you run the
# # provision script and add the key value via gcloud secrets versions add.
# resource "google_secret_manager_secret" "scheduler_mcp_key" {
#   project   = google_project.agent_project.project_id
#   secret_id = "${var.bot_account_id}-scheduler-mcp-key"
#
#   replication {
#     auto {}
#   }
#
#   depends_on = [
#     google_project_service.secretmanager
#   ]
# }
#
# # Grant the AGENT'S default compute SA (which Vertex AI Reasoning Engine
# # runs as by default) read access to the secret. If you deploy with a
# # custom --service-account, replace the member below with that SA.
# resource "google_secret_manager_secret_iam_member" "scheduler_mcp_key_agent_accessor" {
#   project   = google_project.agent_project.project_id
#   secret_id = google_secret_manager_secret.scheduler_mcp_key.secret_id
#   role      = "roles/secretmanager.secretAccessor"
#   member    = "serviceAccount:${google_project.agent_project.number}-compute@developer.gserviceaccount.com"
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

output "service_account_email" {
  description = "Service account email for the agent — share Google Sheets/Drive/Docs with this email; also used for Google Chat when enabled"
  value       = google_service_account.agent.email
}

output "staging_bucket" {
  description = "GCS bucket for ADK deployment staging"
  value       = google_storage_bucket.staging.name
}

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

3a. Create a service account key for the agent SA (the same SA from Section 1
    — used for Sheets/Docs and, with this section enabled, Google Chat):
    gcloud iam service-accounts keys create ${var.bot_account_id}-sa-key.json \
      --iam-account=${google_service_account.agent.email} \
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

    # The webhook URL is per-agent: it must include the agent's Firestore
    # document ID so the middleware can route messages to the correct agent.
    export AGENT_ID=<your-agent-firestore-doc-id>

    # Set the webhook
    curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
      -H "Content-Type: application/json" \
      -d '{
        "url": "https://YOUR_MIDDLEWARE_URL/api/v1/telegram/events/'$AGENT_ID'",
        "secret_token": "'$WEBHOOK_SECRET'"
      }'

    # Save the webhook secret for agent configuration
    echo "Webhook secret: $WEBHOOK_SECRET"

4e. Enable Telegram for your agent in middleware using Firestore console or script

SECTION 5: SCHEDULER MCP SETUP (If using scheduled reminders)

5a. Generate the API key and store its hash on the agent's Firestore doc.
    Run from the middleware repo (NOT this agent repo):
      cd /path/to/slack-vertex-ai-middleware
      python scripts/provision_scheduler_api_key.py --agent-id YOUR_AGENT_FIRESTORE_ID

    The script prints the plaintext key ONCE — copy it for 5b.

5b. Add the plaintext key as a secret value (the empty container was
    created by terraform; this populates it):
      echo -n "PLAINTEXT_FROM_5a" | gcloud secrets versions add \
        ${var.bot_account_id}-scheduler-mcp-key \
        --data-file=- --project=${var.project_id}

5c. Configure your ADK agent to read the key at startup and pass it to
    MCPToolset(StreamableHTTPConnectionParams(headers={"X-API-Key": ...})).
    See FOR_AGENT_DEVELOPERS.md §9 for the full ADK wiring example.

    To rotate: re-run 5a (overwrites the hash in middleware Firestore),
    then re-run 5b with the new plaintext.

SECTION 6: GOOGLE APIS SETUP (Share Google Drive/Sheets)

6a. Share Google Sheets/Drive/Docs files with the agent's service account:
    Service Account Email: ${google_service_account.agent.email}

    Instructions:
    - Open your Google Sheet, Drive file, or Doc
    - Click "Share"
    - Add the service account email above
    - Give it "Editor" or "Viewer" access (depending on agent needs)

    Note: this is the same SA that signs Google Chat messages (when Section 3
    is enabled). One SA, one key in Secret Manager, used for both.

====================================================

EOT
}
