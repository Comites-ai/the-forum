# Agent Deployment - Middleware Integration

This guide covers how to integrate your Vertex AI agent with the multi-platform middleware, supporting Slack, Google Chat, and Telegram.

⚠️ **IMPORTANT**: Copy this file to your agent repository (e.g., `growth-coach-agent/MIDDLEWARE_INTEGRATION.md`) so you see it when working on the agent!

## Quick Start

For multi-platform agents, use the registration script template:

```bash
# Copy the template to your agent repository
cp docs/scripts/register_agent_template.py /path/to/your-agent/register_agent.py

# Configure and run
cd /path/to/your-agent
# Edit register_agent.py with your agent details
python register_agent.py
```

The template handles both the new `platforms` array structure and legacy fields required for backward compatibility.

## Table of Contents

1. [Creating a Brand New Agent - Slack](#creating-a-brand-new-agent---slack)
2. [Creating a Brand New Agent - Google Chat](#creating-a-brand-new-agent---google-chat)
3. [Creating a Brand New Agent - Telegram](#creating-a-brand-new-agent---telegram)
4. [Updating an Existing Agent](#updating-an-existing-agent)
5. [Troubleshooting](#troubleshooting)
6. [Quick Reference](#quick-reference)
7. [Receiving Images from Slack](#receiving-images-from-slack)
8. [Setting Up GCS for Image Storage](#setting-up-gcs-for-image-storage)
9. [Building Scheduled Job Tools](#building-scheduled-job-tools)
10. [Linking Platform Identities](#linking-platform-identities)
11. [Adding MCP Servers to Your Agent (ADK-native)](#adding-mcp-servers-to-your-agent-adk-native)
12. [Scheduler MCP Server](#scheduler-mcp-server)

---

## Creating a Brand New Agent - Slack

Follow these steps when creating a completely new agent and want to make it available via Slack.

### Step 1: Create Slack Bot (5 minutes)

> **IMPORTANT**: After creating your Slack app, ensure it is NOT configured as an "Agent or Assistant" in the **Agents & AI Apps** settings. This mode changes the DM UI to show messages separately instead of as a conversation thread.

```bash
# Navigate to middleware repo
cd /path/to/slack_to_agent_integration

# Option A: Use the template manifest (easiest)
# 1. Copy the template
cp slack-app-manifest.template.yml my-new-agent-manifest.yml

# 2. Edit the manifest (change app name, bot name, etc.)
nano my-new-agent-manifest.yml

# 3. Create the app using Slack CLI
slack apps create -m my-new-agent-manifest.yml
# Follow prompts to name the bot and select workspace

# Option B: Manual creation via web UI
# 1. Go to https://api.slack.com/apps
# 2. Create new app → From an app manifest
# 3. Copy/paste slack-app-manifest.template.yml
# 4. Customize the app name and bot name
```

### Step 2: Install Bot and Get Credentials

```bash
# After creating the app:
# 1. Go to https://api.slack.com/apps → Select your new app
# 2. Navigate to "OAuth & Permissions"
# 3. Click "Install to Workspace"
# 4. Copy the "Bot User OAuth Token" (starts with xoxb-)
# 5. Go to "Basic Information"
# 6. Copy the "Signing Secret"

# Save the token:
export NEW_AGENT_SLACK_BOT_TOKEN="xoxb-your-token-here"

# IMPORTANT: Add the new bot's signing secret to the middleware .env
# The middleware supports multiple comma-separated signing secrets:
# SLACK_SIGNING_SECRET=existing-secret,new-bot-signing-secret
# You must add the new secret BEFORE configuring Event Subscriptions,
# otherwise Slack's URL verification challenge will fail.

# IMPORTANT: Get the correct user_id (NOT the B... bot_id from Slack settings!)
# The middleware uses user_id from Slack's authorizations, which starts with U
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer $NEW_AGENT_SLACK_BOT_TOKEN" | jq .user_id
# Example output: "U0AFZ86NE00"

export NEW_AGENT_SLACK_BOT_ID="U0AFZ86NE00"  # Use the U... ID, not B...
```

### Step 3: Deploy Your Agent to Vertex AI

```bash
# In your agent repository (e.g., my-new-agent/)

# IMPORTANT: ADK requires a staging bucket for deployment artifacts
# Create it once (or let ADK create it automatically):
export PROJECT_ID="your-project-id"
gsutil mb -p ${PROJECT_ID} -l us-central1 gs://${PROJECT_ID}-staging

# Optional: Set lifecycle policy to auto-delete old staging files after 7 days
cat > /tmp/lifecycle.json <<EOF
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {"age": 7}
      }
    ]
  }
}
EOF
gsutil lifecycle set /tmp/lifecycle.json gs://${PROJECT_ID}-staging
rm /tmp/lifecycle.json

# Deploy your agent using ADK
# The staging bucket is used to upload your agent code before deployment
adk deploy agent_engine \
  --project "$PROJECT_ID" \
  --region us-central1 \
  --staging_bucket "gs://${PROJECT_ID}-staging" \
  --display_name "My New Agent" \
  --trace_to_cloud \
  my-agent-directory

# For Vertex AI Reasoning Engines (ADK agents), the ID format is:
# projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID

# Example: After deploying, you'll get an ID like:
# projects/my-project/locations/us-central1/reasoningEngines/7454674542670118912

export NEW_AGENT_VERTEX_ID="projects/YOUR_PROJECT/locations/us-central1/reasoningEngines/YOUR_ENGINE_ID"
```

### Step 4: Register Agent with Middleware

**Option A: Use the Template Registration Script (Recommended)**

```bash
# 1. Copy the template to your agent repository
cp /path/to/slack-vertex-ai-middleware/docs/scripts/register_agent_template.py \
   /path/to/your-agent/register_agent.py

# 2. Edit the configuration section in register_agent.py
# Update: PROJECT_ID, AGENT_NAME, VERTEX_AI_AGENT_ID, SLACK_BOT_ID
# Configure platform-specific settings (Slack, Google Chat, Telegram)

# 3. Run the registration script
cd /path/to/your-agent
python register_agent.py

# This script will:
# 1. Register your agent in Firestore with the platforms array structure
# 2. Include legacy fields for backward compatibility with the middleware
# 3. Support multiple platforms (Slack, Google Chat, Telegram)
# 4. Output confirmation and next steps
```

**Option B: Use the Legacy deploy_agent.py Script (Slack only)**

```bash
cd /path/to/slack_to_agent_integration

python scripts/deploy_agent.py \
  --agent-name "My New Agent" \
  --vertex-ai-agent-id "$NEW_AGENT_VERTEX_ID" \
  --slack-bot-id "$NEW_AGENT_SLACK_BOT_ID" \
  --slack-bot-token "$NEW_AGENT_SLACK_BOT_TOKEN"

# Note: This script only supports Slack and uses the legacy format.
# For multi-platform agents, use Option A.
```

**Important Notes:**
- The middleware currently requires **legacy fields** (`slack_bot_id` at root level) for lookups
- The registration script includes both the new `platforms` array AND legacy fields for compatibility
- Future middleware updates will migrate to platform-based lookups only

### Step 5: Configure Slack Events API

```bash
# The middleware needs to receive messages from Slack

# Get your middleware URL:
# - Local dev: Your ngrok URL (e.g., https://abc123.ngrok.io)
# - Production: Your Cloud Run URL

# Then:
# 1. Go to https://api.slack.com/apps → Your new app
# 2. Navigate to "Event Subscriptions"
# 3. Enable Events
# 4. Set Request URL: https://YOUR_MIDDLEWARE_URL/api/v1/slack/events
# 5. Wait for green checkmark ✓ (verification success)
# 6. Under "Subscribe to bot events", add: message.im
# 7. Click "Save Changes"
# 8. Reinstall the app to workspace if prompted
```

### Step 6: Test Your New Agent

```bash
# 1. Open Slack
# 2. Find your new bot in the Apps section (left sidebar)
# 3. Click on the bot to open a DM
# 4. Send it a message: "Hello!"
# 5. You should get a response from your Vertex AI agent

# Check logs if no response:
# Local development:
#   - Check your terminal running uvicorn for logs

# Production:
gcloud run logs read slack-vertex-middleware \
  --region us-central1 \
  --limit 50
```

### Step 7: Document in Your Agent Repo

```bash
# Copy this file to your agent repo for future reference:
cp /path/to/slack_to_agent_integration/docs/FOR_AGENT_DEVELOPERS.md \
   /path/to/your-agent-repo/MIDDLEWARE_INTEGRATION.md

# Edit MIDDLEWARE_INTEGRATION.md to include agent-specific info:
# - Your agent's Slack bot ID
# - Your agent's display name
# - Vertex AI agent ID
# - Any agent-specific deployment notes
```

**Example agent-specific documentation to add:**

```markdown
# My New Agent - Middleware Integration

## Agent Details
- **Display Name**: My New Agent
- **Slack Bot ID**: B01234567
- **Vertex AI Agent ID**: projects/my-project/locations/us-central1/agents/abc123

## Quick Update Commands

When deploying a new version:

\`\`\`bash
# After deploying to Vertex AI, update middleware:
python /path/to/slack_to_agent_integration/scripts/deploy_agent.py \\
  --agent-name "My New Agent" \\
  --vertex-ai-agent-id "projects/my-project/locations/us-central1/agents/NEW_ID" \\
  --slack-bot-id "B01234567" \\
  --slack-bot-token "$MY_NEW_AGENT_SLACK_TOKEN"
\`\`\`
```

---

## Creating a Brand New Agent - Google Chat

Follow these steps when creating a completely new agent and want to make it available via Google Chat.

### Overview

Google Chat bots have a unique requirement: **each bot needs its own dedicated GCP project**. This is due to Google Chat API restrictions. The middleware provides terraform templates to automate the infrastructure setup.

### Prerequisites

Before starting, ensure you have:
- GCP Organization ID
- Billing Account ID
- Permissions to create projects in your organization
- Docker installed (for running terraform)
- Your agent deployed to Vertex AI (Reasoning Engine)

### Step 1: Set Up Your Agent's Google Chat Infrastructure

Each Google Chat bot requires its own GCP project. We provide terraform templates to automate this.

```bash
# 1. Create a terraform directory in your agent's repository
cd /path/to/your-agent-repo
mkdir google-chat-terraform
cd google-chat-terraform

# 2. Copy the terraform templates from the middleware repo
cp /path/to/slack-vertex-ai-middleware/docs/terraform-templates/agent-project/* .

# 3. Create your terraform.tfvars from the example
cp terraform.tfvars.example terraform.tfvars

# 4. Edit terraform.tfvars with your agent's details
nano terraform.tfvars
```

**Example terraform.tfvars for your agent:**

```terraform
# Copy this and customize for your agent
project_id       = "my-agent-chat-prod"      # Must be globally unique
project_name     = "My Agent Google Chat"
organization_id  = "123456789012"            # Your GCP org ID
billing_account  = "ABCD12-34EF56-7890AB"    # Your billing account
region           = "us-central1"

bot_name         = "My Agent"
bot_account_id   = "my-agent"                # Lowercase, hyphens only
bot_description  = "AI assistant powered by Vertex AI"
bot_avatar_url   = ""                        # Optional: URL to bot avatar image

secret_name      = "my-agent-credentials"    # Name for Secret Manager secret
```

### Step 2: Deploy the Infrastructure with Terraform

```bash
# Still in your-agent-repo/google-chat-terraform/

# Run terraform using Docker (works on all platforms including ARM64)
TERRAFORM_IMAGE="hashicorp/terraform:1.5"

# Initialize terraform
docker run --rm \
    -v "$(pwd):/workspace" \
    -v "$HOME/.config/gcloud:/root/.config/gcloud" \
    -w /workspace \
    $TERRAFORM_IMAGE init

# Review the plan
docker run --rm \
    -v "$(pwd):/workspace" \
    -v "$HOME/.config/gcloud:/root/.config/gcloud" \
    -w /workspace \
    $TERRAFORM_IMAGE plan

# Apply the configuration
docker run --rm -it \
    -v "$(pwd):/workspace" \
    -v "$HOME/.config/gcloud:/root/.config/gcloud" \
    -w /workspace \
    $TERRAFORM_IMAGE apply

# Terraform will create:
# - New GCP project for your Google Chat bot
# - Service account for the bot
# - Required API enablements (Chat, Drive, Sheets if uncommented)
# - Organization policy to allow service account key creation
# - Output with next steps
```

### Step 3: Create and Store Service Account Key

After terraform completes, you'll see output with next steps. Follow them to create the service account key:

```bash
# Get the service account email from terraform output
export BOT_SA_EMAIL=$(docker run --rm \
    -v "$(pwd):/workspace" \
    -v "$HOME/.config/gcloud:/root/.config/gcloud" \
    -w /workspace \
    $TERRAFORM_IMAGE output -raw service_account_email)

export BOT_PROJECT_ID=$(docker run --rm \
    -v "$(pwd):/workspace" \
    -v "$HOME/.config/gcloud:/root/.config/gcloud" \
    -w /workspace \
    $TERRAFORM_IMAGE output -raw project_id)

# Create the service account key
gcloud iam service-accounts keys create my-agent-sa-key.json \
  --iam-account=$BOT_SA_EMAIL \
  --project=$BOT_PROJECT_ID

# Store it in the middleware project's Secret Manager
# Replace 'vertex-ai-middleware-prod' with your middleware project ID
gcloud secrets versions add my-agent-credentials \
  --data-file=my-agent-sa-key.json \
  --project=vertex-ai-middleware-prod

# IMPORTANT: Delete the local key file for security
rm -f my-agent-sa-key.json

# Grant the middleware's Cloud Run service account access to this secret
# (if not already done in middleware terraform)
export MIDDLEWARE_PROJECT_ID="vertex-ai-middleware-prod"
export MIDDLEWARE_PROJECT_NUMBER=$(gcloud projects describe $MIDDLEWARE_PROJECT_ID --format="value(projectNumber)")
export MIDDLEWARE_SA="${MIDDLEWARE_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding my-agent-credentials \
  --member="serviceAccount:${MIDDLEWARE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$MIDDLEWARE_PROJECT_ID
```

### Step 4: Configure Google Chat Bot

Now configure the Google Chat bot in the GCP Console:

```bash
# Open the Google Chat configuration page
echo "Go to: https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=$BOT_PROJECT_ID"

# Then follow these steps:
# 1. Click "Configuration"
# 2. Fill in bot details:
#    - Bot name: My Agent (use your bot name)
#    - Avatar URL: (optional, your bot's avatar image)
#    - Description: AI assistant powered by Vertex AI
# 3. Functionality:
#    - ✓ Receive 1:1 messages
#    - ✓ Join spaces and group conversations
# 4. Connection settings:
#    - Select "App URL"
#    - Bot URL: https://YOUR_MIDDLEWARE_URL/api/v1/google-chat/events
#    - Example: https://slack-vertex-middleware-404939446326.us-central1.run.app/api/v1/google-chat/events
# 5. Permissions:
#    - "Specific people and groups" (add test users for now)
#    - Or "Anyone in your domain" for broader access
# 6. Click "Save"
```

### Step 5: Enable Google Chat in Middleware

Register your agent with the middleware and enable the Google Chat platform:

```bash
cd /path/to/slack-vertex-ai-middleware

# Get your agent's Firestore ID
# If you don't know it, list all agents:
gcloud firestore documents list agents --project=vertex-ai-middleware-prod

# Enable Google Chat for your agent
python scripts/enable_google_chat_agent.py \
  --project vertex-ai-middleware-prod \
  --agent-id "YOUR_AGENT_FIRESTORE_ID" \
  --secret-name "my-agent-credentials" \
  --google-chat-project-id "$BOT_PROJECT_ID"

# This script will:
# 1. Add Google Chat platform configuration to your agent in Firestore
# 2. Reference the service account credentials secret
# 3. Enable the platform
```

### Step 6: Share Google Sheets (If Needed)

If your agent uses Google Sheets (via tools or Reasoning Engine), share those sheets with the bot's service account:

```bash
# The service account email is in the terraform output:
echo $BOT_SA_EMAIL

# Share your Google Sheets with this email address:
# - Open the Google Sheet
# - Click "Share"
# - Add the service account email
# - Give it "Editor" or "Viewer" access (depending on needs)
```

### Step 7: Test Your Google Chat Bot

```bash
# 1. Open Google Chat (web or mobile app)
# 2. Click "+" to start a new chat
# 3. Search for your bot name (e.g., "My Agent")
# 4. Send it a test message: "Hello!"
# 5. You should get a response from your Vertex AI agent

# Check logs if no response:
gcloud run logs read slack-vertex-middleware \
  --project vertex-ai-middleware-prod \
  --region us-central1 \
  --limit 50
```

### What Happens Behind the Scenes

When a user messages your Google Chat bot:

1. Google Chat sends the event to the middleware's `/api/v1/google-chat/events` endpoint
2. Middleware looks up your agent in Firestore (using the Google Chat space info)
3. Middleware retrieves the service account credentials from Secret Manager
4. Middleware creates/retrieves a session for the user
5. Middleware sends the message to your Vertex AI agent
6. Middleware streams the response back to Google Chat using the bot's service account

### Platform Configuration in Firestore

After running `enable_google_chat_agent.py`, your agent document in Firestore will have a `platforms` array like this:

```json
{
  "name": "My Agent",
  "vertex_ai_agent_id": "projects/.../reasoningEngines/...",
  "platforms": [
    {
      "platform": "google_chat",
      "enabled": true,
      "google_chat_service_account_secret": "my-agent-credentials"
    }
  ]
}
```

You can have multiple platforms enabled (e.g., both Slack and Google Chat).

### Troubleshooting Google Chat Setup

**Bot doesn't appear in Google Chat search:**
- Verify the bot is configured in the correct GCP project
- Check "Permissions" allows your test users
- Wait a few minutes for Google's systems to index the bot

**"The bot didn't respond" error:**
- Check middleware logs for errors
- Verify the bot URL is correct in Google Chat configuration
- Ensure the middleware is deployed and accessible
- Verify the secret name matches in both terraform and enable script

**Permission denied errors:**
- Verify the service account key is stored in Secret Manager
- Check the middleware's Cloud Run SA has `secretAccessor` role
- Ensure Google Sheets are shared with the bot's service account

**Agent responds but can't access Google Sheets:**
- Verify you shared the sheets with the bot's service account email
- Check the bot has "Editor" or "Viewer" permissions on the sheets
- Verify Drive and Sheets APIs are enabled (uncomment in terraform if needed)

### Documentation

Keep this terraform configuration in your agent repository:

```bash
# Your agent repo structure should look like:
your-agent-repo/
├── google-chat-terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── terraform.tfvars      # Your configuration (gitignored)
│   ├── terraform.tfvars.example
│   └── README.md
├── agent.py                   # Your agent code
└── MIDDLEWARE_INTEGRATION.md  # Copy of this guide
```

---

## Creating a Brand New Agent - Telegram

Follow these steps when creating a completely new agent and want to make it available via Telegram.

### Overview

Telegram bots use the Telegram Bot API and communicate via webhooks. Unlike Slack and Google Chat, Telegram bots:
- Don't require separate GCP projects
- Use simple bot tokens from @BotFather
- Support webhook secret tokens for security
- Have straightforward file handling

### Prerequisites

Before starting, ensure you have:
- Telegram account (personal account, no business account needed)
- Your agent deployed to Vertex AI (Reasoning Engine)
- Access to the middleware's GCP project

### Step 1: Create Telegram Bot via BotFather

```bash
# 1. Open Telegram and search for @BotFather (official Telegram bot)
# 2. Start a conversation with @BotFather
# 3. Send command: /newbot
# 4. Follow the prompts:
#    - Choose a display name (e.g., "Growth Coach Bot")
#    - Choose a username (must end in 'bot', e.g., "growth_coach_bot")
# 5. BotFather will respond with your bot token

# Example bot token format:
# 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz

export TELEGRAM_BOT_TOKEN="YOUR_BOT_TOKEN_FROM_BOTFATHER"

# IMPORTANT: Keep this token secret! It has full access to your bot.
```

### Step 2: Store Bot Token in Secret Manager

For production, store the token in your agent's GCP project:

```bash
# If you have an existing agent project with terraform
cd /path/to/your-agent-repo/terraform

# Uncomment SECTION 4: TELEGRAM in main.tf
# Then apply terraform to create the secret
terraform apply

# Add the bot token to Secret Manager
export PROJECT_ID=$(terraform output -raw project_id)
export BOT_ACCOUNT_ID="your-agent"  # From terraform.tfvars

echo -n "$TELEGRAM_BOT_TOKEN" | gcloud secrets versions add ${BOT_ACCOUNT_ID}-telegram-token \
  --data-file=- \
  --project=$PROJECT_ID

# Grant middleware access to the secret
export MIDDLEWARE_PROJECT_ID="vertex-ai-middleware-prod"
export MIDDLEWARE_PROJECT_NUMBER=$(gcloud projects describe $MIDDLEWARE_PROJECT_ID --format="value(projectNumber)")
export MIDDLEWARE_SA="${MIDDLEWARE_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding ${BOT_ACCOUNT_ID}-telegram-token \
  --member="serviceAccount:${MIDDLEWARE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT_ID
```

### Step 3: Configure Telegram Webhook

Set up the webhook to point to your middleware:

```bash
# Generate a secure random secret for webhook verification
export WEBHOOK_SECRET=$(openssl rand -base64 32)

# Set the webhook
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"https://YOUR_MIDDLEWARE_URL/api/v1/telegram/events\",
    \"secret_token\": \"${WEBHOOK_SECRET}\"
  }"

# Response should be: {"ok":true,"result":true,...}

# Verify webhook is set
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"

# Save the webhook secret for agent configuration
echo "Webhook Secret: $WEBHOOK_SECRET"
# Store this - you'll need it for the agent config
```

### Step 4: Register Agent with Middleware

Add Telegram platform configuration to your agent in Firestore:

```bash
# Option A: Using Firestore Console (easiest for adding to existing agent)
# 1. Go to: https://console.firebase.google.com/project/vertex-ai-middleware-prod/firestore
# 2. Navigate to: agents collection → your agent document
# 3. Add to the 'platforms' array:
{
  "platform": "telegram",
  "enabled": true,
  "telegram_bot_token_secret": "your-agent-telegram-token",
  "telegram_bot_token_project_id": "your-agent-project-id",
  "telegram_webhook_secret": "THE_WEBHOOK_SECRET_FROM_STEP_3"
}

# Option B: Using a script (create your own based on enable_google_chat_agent.py)
# The middleware doesn't have a pre-built script for Telegram yet,
# but you can manually add the platform config via Firestore console
```

**Agent Firestore Structure Example:**
```json
{
  "display_name": "Growth Coach",
  "vertex_ai_agent_id": "projects/YOUR_PROJECT/locations/us-central1/reasoningEngines/123",
  "platforms": [
    {
      "platform": "slack",
      "enabled": true,
      "slack_bot_id": "U0123456",
      "slack_bot_token_secret": "growth-coach-slack-token",
      "slack_bot_token_project_id": "growth-coach-prod"
    },
    {
      "platform": "telegram",
      "enabled": true,
      "telegram_bot_token_secret": "growth-coach-telegram-token",
      "telegram_bot_token_project_id": "growth-coach-prod",
      "telegram_webhook_secret": "your-webhook-secret-from-step-3"
    }
  ]
}
```

### Step 5: Test Your Telegram Bot

```bash
# 1. Open Telegram on your phone or desktop
# 2. Search for your bot username (e.g., @growth_coach_bot)
# 3. Start a conversation by clicking "Start" or sending /start
# 4. Send it a test message: "Hello!"
# 5. You should get a response from your Vertex AI agent

# Check logs if no response:
gcloud run logs read slack-vertex-middleware \
  --project vertex-ai-middleware-prod \
  --region us-central1 \
  --limit 50 \
  | grep -i telegram
```

### Step 6: Link Your Telegram Identity (Optional)

If you already have a user in the system from Slack or Google Chat, link your Telegram identity:

```bash
cd /path/to/slack-vertex-ai-middleware

# First, send a message to the bot so it creates your Telegram user
# Then check what user ID was created
python scripts/check_user_identities.py

# Link your Telegram identity to your existing user
python scripts/link_identities.py \
  --user-id YOUR_EXISTING_USER_ID \
  --platform telegram \
  --platform-user-id YOUR_TELEGRAM_USER_ID \
  --display-name "Your Name"

# Now you can message the bot from Telegram and it will recognize you
# as the same person across all platforms!
```

### What Happens Behind the Scenes

When a user messages your Telegram bot:

1. Telegram sends the update to the middleware's `/api/v1/telegram/events` endpoint
2. Middleware verifies the webhook secret token
3. Middleware looks up your agent in Firestore (finds first enabled Telegram agent)
4. Middleware retrieves the bot token from Secret Manager
5. Middleware creates/retrieves a session for the user
6. Middleware sends the message to your Vertex AI agent
7. Middleware posts the response back to Telegram using the bot token

### Platform Configuration in Firestore

After configuration, your agent document in Firestore has a `platforms` array:

```json
{
  "name": "My Agent",
  "vertex_ai_agent_id": "projects/.../reasoningEngines/...",
  "platforms": [
    {
      "platform": "telegram",
      "enabled": true,
      "telegram_bot_token_secret": "my-agent-telegram-token",
      "telegram_bot_token_project_id": "my-agent-prod",
      "telegram_webhook_secret": "abc123..."
    }
  ]
}
```

You can have multiple platforms enabled (e.g., Slack + Google Chat + Telegram).

### Troubleshooting Telegram Setup

**Bot doesn't respond to messages:**
- Verify webhook is set correctly: `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
- Check webhook URL points to correct middleware: `https://YOUR_MIDDLEWARE/api/v1/telegram/events`
- Verify secret token matches in webhook config and Firestore
- Check middleware logs for errors

**"403 Permission Denied" errors:**
- Ensure middleware SA has `secretAccessor` role on the bot token secret
- Verify secret exists: `gcloud secrets describe my-agent-telegram-token --project=my-project`

**Webhook verification failed:**
- Check that `telegram_webhook_secret` in Firestore matches the secret token you used in `setWebhook`
- The secret is sent in the `X-Telegram-Bot-Api-Secret-Token` header

**Bot token invalid:**
- Get a new token from @BotFather: `/token` command
- Update the secret in Secret Manager
- Restart the middleware to pick up the new token

**Messages not reaching agent:**
- Check middleware logs: `gcloud run logs read slack-vertex-middleware --limit=50 | grep telegram`
- Verify agent is enabled: Check `platforms[].enabled` in Firestore
- Ensure Vertex AI agent ID is correct

### Telegram Bot Features

**Supported:**
- ✅ Text messages
- ✅ Photos (downloaded and sent to agent)
- ✅ Documents (downloaded and sent to agent)
- ✅ Videos (downloaded and sent to agent)
- ✅ Voice messages (downloaded and sent to agent)
- ✅ Cross-platform identity (same user across Slack/Google Chat/Telegram)
- ✅ Markdown formatting in bot responses

**Not Yet Supported:**
- ❌ Inline keyboards/buttons (Telegram-specific UI)
- ❌ Bot commands (/start, /help, etc.) - treated as regular messages
- ❌ Group chats (only DMs currently)
- ❌ Telegram-specific features (stickers, polls, etc.)

### Documentation

Keep this terraform configuration in your agent repository:

```bash
# Your agent repo structure should look like:
your-agent-repo/
├── terraform/
│   ├── main.tf                     # With SECTION 4: TELEGRAM uncommented
│   ├── variables.tf
│   ├── terraform.tfvars            # Your configuration (gitignored)
│   ├── terraform.tfvars.example
│   └── README.md
├── agent.py                        # Your agent code
└── MIDDLEWARE_INTEGRATION.md       # Copy of this guide
```

---

## Updating an Existing Agent

When you deploy a new version of an existing agent to Vertex AI, you need to update the middleware.

### Quick Update (2 minutes)

1. **Deploy to Vertex AI** and get the new agent ID:

   ```bash
   # In your agent repository
   gcloud ai agents deploy --agent-file=agent.yaml --location=us-central1

   # Output will show:
   # Agent deployed: projects/YOUR_PROJECT/locations/us-central1/agents/NEW_ID

   # Copy this ID
   export NEW_VERTEX_AI_AGENT_ID="projects/YOUR_PROJECT/locations/us-central1/agents/NEW_ID"
   ```

2. **Update the middleware**:

   ```bash
   cd /path/to/slack_to_agent_integration

   python scripts/deploy_agent.py \
     --agent-name "Growth Coach" \
     --vertex-ai-agent-id "$NEW_VERTEX_AI_AGENT_ID" \
     --slack-bot-id "B01234567" \
     --slack-bot-token "$GROWTH_COACH_SLACK_TOKEN"
   ```

3. **Verify the update**:

   ```bash
   # Send a test DM to the bot in Slack
   # Check that it responds with the new agent version behavior

   # Check Firestore to verify update:
   gcloud firestore documents list agents --limit=10
   ```

### What This Does

The `deploy_agent.py` script updates Firestore so the middleware knows to route messages to your new agent version.

**Without this step**, Slack messages will still go to the OLD agent version!

---

## Troubleshooting

### Bot doesn't respond to messages

**Check Firestore**: Verify agent is registered with correct bot_id

```bash
# View Firestore collections
gcloud firestore collections list

# View agents in Firestore
gcloud firestore documents list agents

# Or use Firebase Console:
# https://console.firebase.google.com/project/YOUR_PROJECT/firestore
```

**Check Slack Events**: Ensure Request URL is verified (green checkmark)
- Go to https://api.slack.com/apps → Your app → Event Subscriptions
- Verify the Request URL shows a green checkmark

**Check logs**:

```bash
# Local development:
# Check your terminal running uvicorn

# Production:
gcloud run logs read slack-vertex-middleware \
  --region us-central1 \
  --limit 50 \
  --format json
```

### "Agent not found" error

**Verify agent ID is correct:**

```bash
# List all agents in Vertex AI
gcloud ai agents list --location=us-central1

# Get details of a specific agent
gcloud ai agents describe AGENT_ID --location=us-central1
```

**Check agent deployed successfully:**
- Ensure deployment completed without errors
- Verify using the Vertex AI Console

**Ensure you're using the full agent resource name:**
- Format: `projects/PROJECT_ID/locations/LOCATION/agents/AGENT_ID`
- Not just `AGENT_ID`

### "Slack bot token invalid"

**Get fresh token:**
1. Go to https://api.slack.com/apps → Your app
2. Navigate to "OAuth & Permissions"
3. Copy the "Bot User OAuth Token" (starts with `xoxb-`)
4. Ensure you're copying the entire token

**Verify token format:**
```bash
# Token should start with xoxb-
echo $SLACK_BOT_TOKEN | grep "^xoxb-"
```

**Check token hasn't been revoked:**
- In Slack app settings, check if app is still installed to workspace
- Try reinstalling the app if needed

### "URL verification failed" (Slack)

**Check signing secret is included in the middleware config:**
```bash
# In middleware repo .env file
grep SLACK_SIGNING_SECRET .env

# SLACK_SIGNING_SECRET supports comma-separated values (one per Slack app).
# Your new bot's signing secret must be in this list BEFORE configuring
# Event Subscriptions, otherwise the URL verification challenge will fail.
# Find each secret at: https://api.slack.com/apps → Your app → Basic Information
```

**Ensure middleware is running and accessible:**
```bash
# Test health endpoint
curl https://YOUR_MIDDLEWARE_URL/health

# Should return: {"status":"healthy"}
```

**For ngrok:** Make sure tunnel is active

```bash
# Check ngrok is running
curl http://localhost:4040/api/tunnels

# Should show active tunnel
```

### No response but no errors

**Check Vertex AI agent is responding:**
- Test directly in Vertex AI Console
- Send a test query to verify agent works independently

**Verify session management:**
```bash
# Check Firestore sessions collection
gcloud firestore documents list sessions --limit=10
```

**Check Slack bot has correct scopes:**
- Go to https://api.slack.com/apps → Your app → OAuth & Permissions
- Verify bot has these scopes:
  - `chat:write`
  - `im:history`
  - `im:read`

---

## Full Documentation

See the middleware repo for complete documentation:

- **Repository**: [Your GitHub Repo URL]
- **Main README**: [Your Repo]/README.md
- **Deployment Guide**: [Your Repo]/docs/AGENT_DEPLOYMENT.md
- **Slack Setup**: [Your Repo]/docs/SLACK_SETUP.md
- **GCP Setup**: [Your Repo]/docs/GCP_SETUP.md

---

## Quick Reference

### Create New Agent - Slack

```bash
# 1. Create Slack bot (use template manifest)
slack apps create -m slack-app-manifest.template.yml

# 2. Install to workspace and get credentials
# (via Slack web UI - get token starting with xoxb-)

# 3. Get the correct user_id for the bot (IMPORTANT: use U..., not B...)
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer xoxb-your-token" | jq .user_id

# 4. Deploy agent to Vertex AI (for Reasoning Engines)
# Get the reasoningEngines ID from your deployment

# 5. Register with middleware
python scripts/deploy_agent.py \
  --agent-name "Agent Name" \
  --vertex-ai-agent-id "projects/.../reasoningEngines/ID" \
  --slack-bot-id "U..." \
  --slack-bot-token "xoxb-..."

# 6. Configure Slack Events API
# (Set Request URL via Slack web UI)

# 7. Test with DM in Slack
```

### Create New Agent - Google Chat

```bash
# 1. Copy terraform templates to your agent repo
mkdir your-agent-repo/google-chat-terraform
cp docs/terraform-templates/agent-project/* your-agent-repo/google-chat-terraform/

# 2. Configure terraform.tfvars
cd your-agent-repo/google-chat-terraform
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars  # Edit with your values

# 3. Run terraform
TERRAFORM_IMAGE="hashicorp/terraform:1.5"
docker run --rm -v "$(pwd):/workspace" -v "$HOME/.config/gcloud:/root/.config/gcloud" -w /workspace $TERRAFORM_IMAGE init
docker run --rm -it -v "$(pwd):/workspace" -v "$HOME/.config/gcloud:/root/.config/gcloud" -w /workspace $TERRAFORM_IMAGE apply

# 4. Create and store service account key
gcloud iam service-accounts keys create my-agent-sa-key.json \
  --iam-account=SERVICE_ACCOUNT_EMAIL \
  --project=BOT_PROJECT_ID
gcloud secrets versions add my-agent-credentials \
  --data-file=my-agent-sa-key.json \
  --project=vertex-ai-middleware-prod
rm -f my-agent-sa-key.json

# 5. Configure Google Chat bot
# (Use GCP Console - see detailed steps above)

# 6. Enable Google Chat in middleware
python scripts/enable_google_chat_agent.py \
  --project vertex-ai-middleware-prod \
  --agent-id "FIRESTORE_AGENT_ID" \
  --secret-name "my-agent-credentials" \
  --google-chat-project-id "BOT_PROJECT_ID"

# 7. Test in Google Chat
```

### Create New Agent - Telegram

```bash
# 1. Create Telegram bot via @BotFather
# Open Telegram → message @BotFather → /newbot
# Save the bot token

# 2. Uncomment SECTION 4: TELEGRAM in your agent's terraform/main.tf
terraform apply

# 3. Store bot token in Secret Manager
echo -n "YOUR_BOT_TOKEN" | gcloud secrets versions add your-agent-telegram-token \
  --data-file=- --project=your-agent-project

# 4. Grant middleware access
gcloud secrets add-iam-policy-binding your-agent-telegram-token \
  --member="serviceAccount:MIDDLEWARE_SA" \
  --role="roles/secretmanager.secretAccessor" \
  --project=your-agent-project

# 5. Set Telegram webhook
export WEBHOOK_SECRET=$(openssl rand -base64 32)
curl -X POST "https://api.telegram.org/botYOUR_BOT_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://YOUR_MIDDLEWARE_URL/api/v1/telegram/events","secret_token":"'$WEBHOOK_SECRET'"}'

# 6. Add Telegram platform to agent in Firestore
# Use Firestore console to add to platforms array:
# {
#   "platform": "telegram",
#   "enabled": true,
#   "telegram_bot_token_secret": "your-agent-telegram-token",
#   "telegram_bot_token_project_id": "your-agent-project",
#   "telegram_webhook_secret": "THE_WEBHOOK_SECRET_FROM_STEP_5"
# }

# 7. Test by messaging your bot on Telegram
```

### Update Existing Agent

```bash
# 1. Deploy to Vertex AI (get new agent ID)
# For Reasoning Engines, this will be a new reasoningEngines/ID

# 2. Get the correct user_id (if you don't have it saved)
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer xoxb-your-token" | jq .user_id

# 3. Update middleware
python scripts/deploy_agent.py \
  --agent-name "Agent Name" \
  --vertex-ai-agent-id "projects/.../reasoningEngines/NEW_ID" \
  --slack-bot-id "U..." \
  --slack-bot-token "xoxb-..."

# 4. Test with DM in Slack
```

### Check Status

```bash
# View registered agents
gcloud firestore documents list agents

# View active sessions
gcloud firestore documents list sessions

# Check logs
gcloud run logs read slack-vertex-middleware --region us-central1 --limit 50
```

---

## Receiving Images from Slack

The middleware can download images from Slack messages and forward them to your agent. However, **ADK agents are not multimodal by default** - you need to update your agent code to handle images.

### What the Middleware Sends

When a user sends a message (with or without images), the middleware forwards it to your agent with the following structure:

**With GCS configured** (recommended for Agent Engine):

```python
{
    "message": "[From: Jonathan Cavell] What wine pairs with this?",
    "user_id": "Jonathan Cavell",  # User's actual name from Firestore
    "session_id": "Jonathan Cavell:5695302693795397632",
    "images": [
        {
            "gcs_uri": "gs://your-bucket/slack-files/20260328/a1b2c3d4e5f6.png",
            "mime_type": "image/png"
        }
    ]
}
```

**Without GCS** (base64 fallback):

```python
{
    "message": "[From: Jonathan Cavell] What wine pairs with this?",
    "user_id": "Jonathan Cavell",  # User's actual name from Firestore
    "session_id": "Jonathan Cavell:5695302693795397632",
    "images": [
        {
            "data": "iVBORw0KGgoAAAANSUhEUgAA...",  # base64-encoded image
            "mime_type": "image/png"
        }
    ]
}
```

### User Identity Format

**Important**: The middleware sends the user's **actual name** (not platform IDs) to your agent:

- **`user_id`**: The user's primary name from Firestore (e.g., "Jonathan Cavell", "Sarah Johnson")
- **`session_id`**: Combines the user name and Vertex AI session ID (e.g., "Jonathan Cavell:5695302693795397632")
- **`message` prefix**: Includes the user's name for context (e.g., "[From: Jonathan Cavell]")

This means your agent recognizes users by their actual name across **both Slack and Google Chat**, enabling personalized interactions and consistent conversation history regardless of which platform they use.

**Multi-Platform User Identity:**
- Users can message your agent from both Slack and Google Chat
- The middleware maintains a unified user record in Firestore that links both platform identities
- Your agent always receives the same `user_id` (the person's name) regardless of which platform they're using
- This enables seamless cross-platform conversations and consistent personalization

### Prerequisites

1. **Add `files:read` scope** to your Slack bot:
   - Go to https://api.slack.com/apps → Your app → OAuth & Permissions
   - Under Bot Token Scopes, add `files:read`
   - Reinstall the app to your workspace

2. **Use a multimodal model** in your agent (e.g., `gemini-2.0-flash` or `gemini-1.5-pro`)

### Updating Your ADK Agent

By default, ADK agents only process the `message` field. To handle images, you need to modify your agent's `stream_query` method to:

1. Extract images from the input
2. Convert base64 data to Gemini `Part` objects
3. Include them in the prompt to the LLM

### Example Implementation

Here's how to update your agent to process images (supports both GCS URIs and base64):

```python
import base64
from google.cloud import storage
from google.genai import types

def load_image_bytes(img: dict) -> bytes:
    """Load image bytes from either GCS URI or base64 data."""
    if "gcs_uri" in img:
        # Parse gs://bucket/path format
        uri = img["gcs_uri"]
        parts = uri.replace("gs://", "").split("/", 1)
        bucket_name, blob_name = parts[0], parts[1]

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        return blob.download_as_bytes()
    elif "data" in img:
        # Base64 fallback
        return base64.b64decode(img["data"])
    else:
        raise ValueError("Image must have either 'gcs_uri' or 'data' field")


class MyAgent:
    def __init__(self):
        # Use a multimodal model
        self.model = "gemini-2.0-flash"
        # ... rest of initialization

    def stream_query(self, *, message: str, user_id: str, session_id: str = None, images: list = None, **kwargs):
        """
        Process a user query, optionally with images.

        Args:
            message: The user's text message
            user_id: User identifier
            session_id: Session identifier for conversation continuity
            images: Optional list of image dicts with 'gcs_uri' or 'data', and 'mime_type'
        """
        # Build the content parts for the prompt
        content_parts = []

        # Add images first (if any)
        if images:
            for img in images:
                image_bytes = load_image_bytes(img)
                content_parts.append(
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type=img["mime_type"]
                    )
                )

        # Add the text message
        content_parts.append(types.Part.from_text(message))

        # Create the content object
        user_content = types.Content(
            role="user",
            parts=content_parts
        )

        # Send to the model (adjust based on your agent's architecture)
        # This example assumes you're using the Gemini client directly
        response = self.client.models.generate_content_stream(
            model=self.model,
            contents=[user_content],
            # ... your other config
        )

        for chunk in response:
            yield chunk.text
```

### For ADK Agents Using `LlmAgent`

If you're using `google.adk.agents.LlmAgent`, you'll need to customize how content is built. The simplest approach is to override the query handling:

```python
from google.adk.agents import LlmAgent
from google.genai import types
import base64

class MultimodalAgent(LlmAgent):
    def __init__(self, **kwargs):
        super().__init__(
            model="gemini-2.0-flash",  # Must be multimodal
            **kwargs
        )

    async def _build_user_content(self, message: str, images: list = None) -> types.Content:
        """Build user content with optional images."""
        parts = []

        # Add images first
        if images:
            for img in images:
                image_bytes = base64.b64decode(img["data"])
                parts.append(
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type=img["mime_type"]
                    )
                )

        # Add text
        parts.append(types.Part.from_text(message))

        return types.Content(role="user", parts=parts)
```

### Testing Image Support

1. Deploy your updated agent to Vertex AI
2. Update the middleware registration (same Slack bot, new Vertex AI agent ID)
3. Send an image to your bot via Slack DM
4. Check Cloud Run logs for:
   - `"Downloaded image: image/png, XXXXX bytes"` - middleware received image
   - `"Sending 1 image(s) to Reasoning Engine"` - middleware forwarded to your agent

### Troubleshooting

**Agent returns empty response when image is sent:**
- Your agent isn't processing the `images` field - implement the handling above
- Check your model supports vision (use `gemini-2.0-flash` or `gemini-1.5-pro`)

**"I didn't like that request" message:**
- This is the middleware's fallback when the agent returns an empty response
- Usually means the agent doesn't know how to handle the `images` parameter

**Image not appearing in agent input:**
- Verify `files:read` scope is added to your Slack bot
- Check middleware logs for download errors

---

## Setting Up GCS for Image Storage

When `GCS_BUCKET_NAME` is configured, the middleware uploads images to Google Cloud Storage instead of base64-encoding them. This is recommended for Agent Engine and provides better performance for large images.

### Step 1: Create the GCS Bucket

```bash
# Set your project variables
export PROJECT_ID="your-gcp-project"
export BUCKET_NAME="${PROJECT_ID}-slack-files"
export REGION="us-central1"

# Create bucket with uniform access
gcloud storage buckets create gs://${BUCKET_NAME} \
    --project=${PROJECT_ID} \
    --location=${REGION} \
    --uniform-bucket-level-access
```

### Step 2: Set Lifecycle Rule (Auto-Delete After 1 Day)

Images are only needed during the conversation, so we auto-delete them after 1 day:

```bash
cat > /tmp/lifecycle.json << 'EOF'
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 1}
    }
  ]
}
EOF

gcloud storage buckets update gs://${BUCKET_NAME} \
    --lifecycle-file=/tmp/lifecycle.json

# Verify lifecycle is set
gcloud storage buckets describe gs://${BUCKET_NAME} --format="yaml(lifecycle)"
```

### Step 3: Grant IAM Permissions

The middleware needs write access to upload files:

```bash
# Find your Cloud Run service account (default is the Compute Engine SA)
export PROJECT_NUMBER=$(gcloud projects describe ${PROJECT_ID} --format="value(projectNumber)")
export MIDDLEWARE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Or if you use a custom service account for Cloud Run:
# export MIDDLEWARE_SA="your-custom-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# Grant the middleware write access to the bucket
gcloud storage buckets add-iam-policy-binding gs://${BUCKET_NAME} \
    --member="serviceAccount:${MIDDLEWARE_SA}" \
    --role="roles/storage.objectAdmin"
```

### Step 4: Grant Agent Read Access (If Needed)

If your agents run under a different service account, grant them read access:

```bash
export AGENT_SA="your-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud storage buckets add-iam-policy-binding gs://${BUCKET_NAME} \
    --member="serviceAccount:${AGENT_SA}" \
    --role="roles/storage.objectViewer"
```

**Note:** If agents run under the same project's default service account, they likely already have access via project-level permissions.

### Step 5: Configure the Middleware

Add to your middleware's `.env` file:

```bash
GCS_BUCKET_NAME=your-project-slack-files
GCS_FILE_PREFIX=slack-files
```

### Verifying the Setup

1. Send an image to your bot via Slack DM
2. Check middleware logs for: `"Uploaded image to GCS: gs://..."`
3. Verify the file exists:
   ```bash
   gcloud storage ls gs://${BUCKET_NAME}/slack-files/
   ```
4. Verify your agent receives the `gcs_uri` field in the images array

---

## Building Scheduled Job Tools

Your agent can provide tools that allow users to manage their own scheduled jobs through the middleware API. This enables features like:
- "Remind me every morning at 9 AM to review my goals"
- "Show me my scheduled check-ins"
- "Cancel my daily standup reminder"

### Recommended Approach: Custom Functions Module

**Best Practice**: Create a `custom_functions.py` module in your agent that wraps the scheduled jobs API. This approach:
- ✅ Centralizes API logic and configuration
- ✅ Provides clean function signatures for your agent
- ✅ Makes it easy to update when the API changes
- ✅ Keeps your agent code clean and maintainable

See the [Growth Coach example](https://github.com/your-org/growth-coach-agent/blob/main/custom_functions.py) for a complete implementation.

### Scheduled Jobs API

The middleware exposes a REST API for managing scheduled jobs. Your agent calls these endpoints using HTTP requests.

**Base URL**: `https://YOUR_MIDDLEWARE_URL/api/v1/scheduled-jobs`

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/scheduled-jobs` | Create a new scheduled job |
| `GET` | `/scheduled-jobs?user_id={user_id}` | List jobs for a user |
| `GET` | `/scheduled-jobs/{job_id}` | Get a specific job |
| `PATCH` | `/scheduled-jobs/{job_id}` | Update a job |
| `DELETE` | `/scheduled-jobs/{job_id}` | Delete a job |

### Data Model

Each scheduled job has the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable name (e.g., "Daily Goal Review") |
| `prompt` | string | The message sent to the agent when the job runs |
| `agent_id` | string | Your agent's Firestore document ID |
| `user_id` | string | User ID from the users collection (platform-agnostic) |
| `output_platform` | string | Platform to send to: "slack", "google_chat", or "telegram" |
| `schedule` | string | Cron expression (e.g., "0 9 * * 1-5" for 9 AM weekdays) |
| `timezone` | string | IANA timezone (e.g., "America/New_York") |
| `enabled` | boolean | Whether the job is active |

**Important**: The API now uses `user_id` (platform-agnostic) instead of `slack_user_id`. The middleware looks up the platform-specific identity when delivering messages.

### Example: Custom Functions Module

Create a `custom_functions.py` file in your agent repository:

```python
# custom_functions.py
import requests
import os
from typing import List, Dict, Any, Optional

def create_scheduled_reminder(
    name: str,
    prompt: str,
    schedule: str,
    timezone: str,
    user_id: str,
    output_platform: str = "slack"
) -> Dict[str, Any]:
    """
    Create a scheduled reminder that messages the user on a recurring schedule.

    Args:
        name: Friendly name for this reminder (e.g., "Morning Goals Check-in")
        prompt: The message that will be sent to start the conversation
        schedule: Cron expression (e.g., "0 9 * * 1-5" for 9 AM weekdays)
        timezone: IANA timezone (e.g., "America/New_York", "America/Los_Angeles")
        user_id: The user's ID from the users collection (platform-specific ID is looked up)
        output_platform: Platform to send responses to ("slack", "google_chat", or "telegram")

    Returns:
        The created job details including its ID
    """
    middleware_url = os.environ.get("MIDDLEWARE_URL")
    agent_id = os.environ.get("YOUR_AGENT_ID")  # e.g., GROWTH_COACH_AGENT_ID

    response = requests.post(
        f"{middleware_url}/api/v1/scheduled-jobs",
        json={
            "name": name,
            "prompt": prompt,
            "agent_id": agent_id,
            "user_id": user_id,
            "output_platform": output_platform,
            "schedule": schedule,
            "timezone": timezone,
            "enabled": True
        },
        timeout=30
    )
    response.raise_for_status()
    return response.json()


def list_scheduled_reminders(user_id: str) -> List[Dict[str, Any]]:
    """
    List all scheduled reminders for the user.

    Args:
        user_id: The user's ID from the users collection

    Returns:
        List of scheduled jobs with their details (id, name, schedule, etc.)
    """
    middleware_url = os.environ.get("MIDDLEWARE_URL")
    agent_id = os.environ.get("YOUR_AGENT_ID")

    response = requests.get(
        f"{middleware_url}/api/v1/scheduled-jobs",
        params={"user_id": user_id, "agent_id": agent_id},
        timeout=30
    )
    response.raise_for_status()
    return response.json().get("jobs", [])


def update_scheduled_reminder(
    job_id: str,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    schedule: Optional[str] = None,
    timezone: Optional[str] = None,
    enabled: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Update an existing scheduled reminder.

    Args:
        job_id: The job ID (from list_scheduled_reminders)
        name: New name (optional)
        prompt: New prompt message (optional)
        schedule: New cron schedule (optional)
        timezone: New timezone (optional)
        enabled: Enable/disable the job (optional)

    Returns:
        Updated job details
    """
    middleware_url = os.environ.get("MIDDLEWARE_URL")

    updates = {}
    if name is not None:
        updates["name"] = name
    if prompt is not None:
        updates["prompt"] = prompt
    if schedule is not None:
        updates["schedule"] = schedule
    if timezone is not None:
        updates["timezone"] = timezone
    if enabled is not None:
        updates["enabled"] = enabled

    response = requests.patch(
        f"{middleware_url}/api/v1/scheduled-jobs/{job_id}",
        json=updates,
        timeout=30
    )
    response.raise_for_status()
    return response.json()


def delete_scheduled_reminder(job_id: str) -> Dict[str, Any]:
    """
    Delete a scheduled reminder.

    Args:
        job_id: The job ID (from list_scheduled_reminders)

    Returns:
        Confirmation with job_id and success status
    """
    middleware_url = os.environ.get("MIDDLEWARE_URL")

    response = requests.delete(
        f"{middleware_url}/api/v1/scheduled-jobs/{job_id}",
        timeout=30
    )

    return {"success": response.status_code == 204, "job_id": job_id}
```

Then import these functions in your agent:

```python
# agent.py
from custom_functions import (
    create_scheduled_reminder,
    list_scheduled_reminders,
    update_scheduled_reminder,
    delete_scheduled_reminder
)
from google.adk.tools import FunctionTool

# Create tools from the functions
create_reminder_tool = FunctionTool(func=create_scheduled_reminder)
list_reminders_tool = FunctionTool(func=list_scheduled_reminders)
update_reminder_tool = FunctionTool(func=update_scheduled_reminder)
delete_reminder_tool = FunctionTool(func=delete_scheduled_reminder)

# Add to your agent's tools list
tools = [
    create_reminder_tool,
    list_reminders_tool,
    update_reminder_tool,
    delete_reminder_tool,
    # ... your other tools
]
```

### Configuration

Your agent needs the following environment variables to access the API:

```python
# In your agent's deployment configuration
MIDDLEWARE_URL = "https://your-middleware.run.app"
YOUR_AGENT_ID = "your-agent-firestore-id"  # Document ID from agents collection
```

You can get your agent's Firestore ID by:

```bash
gcloud firestore documents list agents --project=vertex-ai-middleware-prod
```

### Cron Expression Reference

| Schedule | Cron Expression |
|----------|-----------------|
| Every day at 9 AM | `0 9 * * *` |
| Weekdays at 9 AM | `0 9 * * 1-5` |
| Every Monday at 10 AM | `0 10 * * 1` |
| Every hour | `0 * * * *` |
| Every 30 minutes | `*/30 * * * *` |
| First day of month at noon | `0 12 1 * *` |

Format: `minute hour day-of-month month day-of-week`

### Security Considerations

1. **User Ownership**: Jobs are filtered by `user_id` - users can only see/modify their own jobs
2. **Agent Scope**: Include `agent_id` filter when listing to show only jobs for your agent
3. **Validation**: The API validates cron expressions and timezone values
4. **Cross-Platform**: The `user_id` is platform-agnostic, but `output_platform` determines delivery

### Accessing User Context

When a scheduled job executes, the prompt is sent to your agent with the user's identity:

```
[From: Jonathan Cavell] What should I focus on today?
```

The user's actual name (from Firestore) is included in both the message prefix and the `user_id` field, allowing your agent to personalize responses and access user-specific data consistently across all platforms.

---

## Linking Platform Identities

The middleware supports **cross-platform user identity**, allowing the same person to message your agent from Slack, Google Chat, and Telegram while maintaining a unified conversation history and personalization.

### How Identity Resolution Works

When a message arrives:

1. **Auto-creation**: If the platform user is new, middleware creates a user with that platform identity
2. **Email linking**: If a user with the same email exists, identities are automatically linked
3. **Manual linking**: Use `link_identities.py` to merge identities for the same person

### Use Cases for Manual Linking

**Scenario 1: User messages from new platform**
- Jonathan uses Growth Coach on Slack (user ID: `abc123`)
- Jonathan messages Growth Coach on Telegram for the first time
- New Telegram-only user is created (user ID: `xyz789`)
- Use `link_identities.py` to merge Telegram identity into existing user `abc123`

**Scenario 2: Email auto-linking didn't work**
- Slack provides email, Google Chat provides email → auto-linked ✅
- Telegram doesn't provide email → manual link required

**Scenario 3: Same person, different email addresses**
- Work Slack uses `jonathan@company.com`
- Personal Google Chat uses `jonathan@gmail.com`
- Both are the same person → manually link

### Linking Identities with the Script

```bash
cd /path/to/slack-vertex-ai-middleware

# Step 1: Find the user IDs
python scripts/check_user_identities.py

# Example output:
# User abc123:
#   Name: Jonathan Cavell
#   Email: jonathan@company.com
#   Identities:
#     - slack: U0ABC123 (Jonathan Cavell)
#     - google_chat: users/123456 (Jonathan)
#
# User xyz789:
#   Name: Jonathan
#   Identities:
#     - telegram: 987654321 (Jonathan)

# Step 2: Link the Telegram identity to the existing user
python scripts/link_identities.py \
  --user-id abc123 \
  --platform telegram \
  --platform-user-id 987654321 \
  --display-name "Jonathan"

# Output:
# ✓ Added telegram identity to user abc123
# User now has 3 platform identities:
#   - slack: U0ABC123 (Jonathan Cavell)
#   - google_chat: users/123456 (Jonathan)
#   - telegram: 987654321 (Jonathan)
```

### Script Parameters

- `--user-id`: The Firestore user document ID to add the identity to (keep this user)
- `--platform`: Platform name (`slack`, `google_chat`, `telegram`)
- `--platform-user-id`: Platform-specific user ID (from the duplicate user)
- `--display-name`: User's display name on that platform
- `--project-id`: Optional, defaults to `vertex-ai-middleware-prod`

### Benefits of Linked Identities

1. **Unified Conversations**: Same session across all platforms
2. **Consistent Personalization**: Agent recognizes you everywhere
3. **Centralized History**: All interactions in one place
4. **Flexible Communication**: Use whichever platform is convenient

### Example: Cross-Platform Experience

```
Monday 9 AM - Slack:
  You: "What should I focus on today?"
  Bot: "Based on your goals, prioritize the marketing proposal."

Tuesday 3 PM - Telegram (on your phone):
  You: "How did the marketing proposal go?"
  Bot: "I don't have an update yet. Let me know when you complete it!"

Wednesday 10 AM - Google Chat (on web):
  You: "I finished the marketing proposal!"
  Bot: "Great work, Jonathan! What's next on your list?"
```

All three conversations are part of the same session because your identities are linked.

### Checking Current Identities

```bash
# View all users and their linked identities
python scripts/check_user_identities.py

# View specific user by Firestore ID
gcloud firestore documents describe abc123 \
  --collection=users \
  --project=vertex-ai-middleware-prod
```

### Unlinking Identities

If you need to unlink an identity (rare), edit the user document in Firestore:

1. Go to Firestore console
2. Navigate to `users` → your user document
3. Edit the `identities` array
4. Remove the unwanted identity entry
5. Update `updated_at` timestamp

---

**Remember**: Save this file in your agent repo so it's always available when working on the agent!

---

## Adding MCP Servers to Your Agent (ADK-native)

The middleware **does not proxy general-purpose MCP servers** (Garmin, GitHub, Filesystem, etc.). Each agent integrates those directly via ADK's `MCPToolset`, owning the connection in its own Reasoning Engine container. This keeps the middleware focused on identity, delivery, and scheduling — not tool routing.

> **The one exception is the scheduler MCP**, which the middleware *does* host because the scheduling logic and data live in the middleware's Firestore. See [Scheduler MCP Server](#scheduler-mcp-server) below.

### Why agent-side MCP

- **No middleware changes needed** to add tools — agents own their toolchain end-to-end.
- **Per-user credentials** are easier to handle in agent code, where you already have the user's identity.
- **Failure isolation**: a flaky MCP server only impacts that one agent, not the whole platform.
- **Aligned with ADK design**: `MCPToolset` is a first-class ADK primitive.

### stdio transport (most ecosystem servers)

Most public MCP servers ship as `npx` (Node) or `uvx` (Python) packages. ADK launches them as subprocesses inside your Reasoning Engine container.

```python
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from mcp import StdioServerParameters

github_toolset = MCPToolset(
    connection_params=StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": os.environ["GITHUB_PAT"]},
    ),
)

root_agent = LlmAgent(
    model="gemini-2.0-flash",
    tools=[github_toolset, ...],
)
```

**Runtime requirements for stdio:**
- `uvx` (Python packages) — install via your agent's `requirements.txt` (`uv` package).
- `npx` (Node packages) — Node.js must be in the Reasoning Engine container. The default Vertex AI Agent Engine Python runtime does **not** ship Node by default; verify with a smoke test before committing to a Node-based MCP server, or pick a `uvx` equivalent.
- First-call cost: 5-10 seconds while the package is fetched and the subprocess starts. Subsequent calls in the same container instance are fast.

### Streamable HTTP / SSE transport (hosted MCP servers)

If the MCP server runs as its own HTTP service (third-party hosted, or one you deploy yourself), use the HTTP transport:

```python
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, SseServerParams

my_tools = MCPToolset(
    connection_params=SseServerParams(
        url="https://your-mcp-server.example.com/sse",
        headers={"X-API-Key": os.environ["MY_MCP_API_KEY"]},
    ),
)
```

For Streamable HTTP (the modern MCP spec, recommended over SSE for new servers), use the equivalent `StreamableHTTPServerParams` from your ADK / `mcp` SDK version.

### Credentials

Agents own their secrets — store them in Secret Manager in your agent's project and inject at deploy time. The middleware no longer needs to read your MCP credentials.

```bash
# Store the secret in your agent's project
echo -n "ghp_YOUR_TOKEN" | gcloud secrets versions add github-pat \
  --data-file=- --project=$AGENT_PROJECT

# Grant your agent's Reasoning Engine service account access
gcloud secrets add-iam-policy-binding github-pat \
  --member="serviceAccount:${AGENT_RE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$AGENT_PROJECT
```

In your agent code, fetch from Secret Manager at startup (or use a runtime injection mechanism) and pass into `StdioServerParameters`.

### Per-user credentials

If the MCP server needs user-specific credentials (Garmin, Gmail, Calendar, etc.), instantiate the toolset **per request** using the calling user's stored credentials, rather than a process-wide token. The middleware passes the user's identity in the message prefix (`[From: Name | platform_id: ...]`); your agent uses that to look up the right credential before constructing the toolset.

### Verification

Test your MCP integration locally with [`mcp-inspector`](https://github.com/modelcontextprotocol/inspector) before deploying:

```bash
# stdio
npx @modelcontextprotocol/inspector \
  npx -y @modelcontextprotocol/server-github

# HTTP
npx @modelcontextprotocol/inspector \
  sse https://your-mcp-server.example.com/sse
```

---

## Scheduler MCP Server

The middleware hosts a single MCP server — the scheduler — at:

```
POST {middleware_url}/api/v1/mcp/scheduler        (Streamable HTTP, MCP spec 2025-03-26)
```

This is the **one** MCP server the middleware exposes. It wraps the existing `/api/v1/scheduled-jobs` REST API as MCP tools so your agent can manage user reminders directly through the LLM tool loop instead of you maintaining wrapper functions in `custom_functions.py`.

### Why this one is hosted by the middleware

- The scheduling logic already lives in the middleware (`app/services/scheduled_job_service.py`) and the data lives in middleware Firestore. Co-hosting saves a network hop and avoids duplicating the service code in every agent.
- `agent_id` is auto-resolved from the API key — your LLM never has to learn its own ID, which removes a category of tool-call mistakes.
- Authorization (jobs filtered by the calling agent) is enforced server-side.

### Tools exposed

| Tool | Inputs | Returns |
|---|---|---|
| `create_scheduled_reminder` | `name`, `prompt`, `schedule` (cron), `user_id`, optional `timezone`, `output_platform` | the new job |
| `list_scheduled_reminders` | `user_id` | array of jobs |
| `update_scheduled_reminder` | `job_id`, optional `name`/`prompt`/`schedule`/`timezone`/`enabled` | updated job |
| `delete_scheduled_reminder` | `job_id` | `{success, job_id}` |

If you don't pass `output_platform` to `create_scheduled_reminder`, it defaults to whichever platform the user most recently chatted with this agent on (falling back to `slack` if there's no session yet).

### Provisioning your agent's API key

Each agent gets its own API key. The middleware stores only a SHA-256 hash; you store the plaintext.

```bash
# Run from the middleware repo
python scripts/provision_scheduler_api_key.py --agent-id YOUR_AGENT_FIRESTORE_ID

# Output (shown ONCE — vault it immediately):
#   Plaintext key (shown ONCE — vault it now):
#       <43-char-token>
```

Then store the plaintext in your agent's project Secret Manager:

```bash
echo -n '<KEY>' | gcloud secrets versions add scheduler-mcp-key \
  --data-file=- --project=$AGENT_PROJECT

# And grant your agent's Reasoning Engine SA access to read it
gcloud secrets add-iam-policy-binding scheduler-mcp-key \
  --member="serviceAccount:${AGENT_RE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$AGENT_PROJECT
```

To rotate, just re-run the provision script — the new hash overwrites the old one in Firestore and the previous plaintext stops working immediately.

### Wiring it into your ADK agent

```python
import os
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StreamableHTTPServerParams

scheduler_toolset = MCPToolset(
    connection_params=StreamableHTTPServerParams(
        url=f"{os.environ['MIDDLEWARE_URL']}/api/v1/mcp/scheduler",
        headers={"X-API-Key": os.environ["SCHEDULER_MCP_KEY"]},
    ),
)

root_agent = LlmAgent(
    model="gemini-2.0-flash",
    tools=[scheduler_toolset, ...],  # plus whatever else your agent has
)
```

Your agent populates `MIDDLEWARE_URL` from its config and `SCHEDULER_MCP_KEY` by reading from Secret Manager at startup.

### What changes vs. the old `custom_functions.py` approach

The pre-MCP guidance recommended writing four wrapper functions (`create_scheduled_reminder`, `list_scheduled_reminders`, `update_scheduled_reminder`, `delete_scheduled_reminder`) using `requests` against the REST API and registering them as `FunctionTool`s. **You don't need any of that anymore** — the MCP server replaces it. Old agents using the REST API still work; the REST endpoints aren't going away. But for new agents, prefer the MCP path.
