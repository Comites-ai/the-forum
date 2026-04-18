# Multi-Platform Vertex AI Agent Middleware

FastAPI middleware service that routes messages from **Slack**, **Google Chat**, and **Telegram** to Google Vertex AI Agent Engine and posts responses back. Supports multiple agents with individual platform identities, automatic session management, cross-platform conversation continuity, and scheduled jobs.

## Features

- **Multi-Platform Support**: Slack, Google Chat, and Telegram with unified architecture
- **Multi-Agent Support**: Each agent has its own identity on each platform
- **Cross-Platform Sessions**: Continue conversations across Slack, Google Chat, and Telegram
- **Session Management**: Automatic session tracking per user+agent combination
- **Scheduled Jobs**: Proactive agent-initiated messages with rate limiting
- **MCP Server Support**: Per-agent MCP tool proxy; agents configure backing MCP servers in Firestore and the middleware aggregates their tools — plus a global owner endpoint for Claude Code and other tools
- **Async Processing**: Responds within 3 seconds, processes in background
- **Infrastructure as Code**: Complete Terraform configuration
- **Secure**: Request signature verification, Secret Manager integration
- **Scalable**: Serverless deployment on Google Cloud Run
- **Easy Setup**: Comprehensive scripts and documentation

## Architecture

### Message Flow

```
User Message
  ↓
Platform (Slack, Google Chat, or Telegram)
  ↓
POST /api/v1/{platform}/events
  ↓
Return 200 OK (< 3s)
  ↓
BackgroundTask:
  ├─ Parse platform event → unified format
  ├─ Identify agent (Firestore lookup)
  ├─ Resolve user identity (cross-platform)
  ├─ Get/create session (Firestore)
  ├─ Send to Vertex AI Reasoning Engine
  └─ Post response via platform connector
```

### MCP Tool Proxy

Each Vertex AI ADK agent can declare backing MCP servers in its Firestore record. The middleware aggregates their tools (prefixed `{server_name}__{tool_name}`) and serves them from a stable per-agent endpoint:

```
ADK Agent
  └── MCPToolset(url="{middleware}/api/v1/mcp/{agent_id}/sse")   ← SSE (ADK compat)
Claude Code / owner tools
  └── MCP server at "{middleware}/api/v1/mcp"  (X-API-Key header) ← Streamable HTTP

                     ▼
  Middleware MCP Proxy  (aggregates tools, prefixes names)
        │
        ├── Backing server A  (e.g. public MCP server)
        └── Backing server B  (custom Cloud Run service)
```

The global owner endpoint (`/api/v1/mcp`) exposes all configured MCP servers across all agents plus any global-only servers stored in the `mcp_servers` Firestore collection. See [docs/USING_MCP_SERVER.md](docs/USING_MCP_SERVER.md) for setup.

### Platform Connectors

The middleware uses a unified `PlatformConnector` interface:
- **SlackConnector**: Slack Events API integration
- **GoogleChatConnector**: Google Chat API with service account auth
- **TelegramConnector**: Telegram Bot API with webhook verification

All platform-specific logic is isolated in connectors, making it easy to add new platforms.

## Tech Stack

- **Framework**: Python 3.11+ (tested with 3.12) + FastAPI
- **Hosting**: Google Cloud Run (serverless)
- **Database**: Google Firestore (agents, sessions, users, scheduled jobs)
- **Agent Runtime**: Google Vertex AI Reasoning Engine
- **Messaging**:
  - Slack Events API (HTTP push)
  - Google Chat API (HTTP push + service account auth)
  - Telegram Bot API (HTTP push + webhook verification)
- **Infrastructure**: Terraform (reproducible infrastructure-as-code)
- **Secrets**: Google Cloud Secret Manager
- **Storage**: Google Cloud Storage (temporary file uploads)
- **Scheduling**: Google Cloud Scheduler (cron-based job dispatcher)
- **MCP**: `mcp` Python SDK (Streamable HTTP + SSE transports; multi-server aggregation proxy)
- **Local Dev**: ngrok for tunneling

## Prerequisites

- Python 3.11 or 3.12
- **Google Workspace Business** account (for Google Chat bots)
- Google Cloud project in Workspace organization
- Slack workspace with admin access (for Slack integration)
- Terraform 1.0+ (for infrastructure deployment)
- ngrok account (free tier, for local development)

## Quick Start

### 1. Clone and Setup

```bash
# Clone repository
git clone <your-repo-url>
cd slack_to_agent_integration

# Create virtual environment (use python3.11 or python3.12)
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
# Note: First install may take a few minutes due to dependency resolution
pip install -r requirements.txt

# Copy environment template
cp .env.example .env

# Edit .env with your values
nano .env
```

### 2. Infrastructure Deployment with Terraform

**Recommended**: Use Terraform for reproducible infrastructure:

```bash
# Navigate to terraform directory
cd terraform

# Copy and configure variables
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your project ID

# Authenticate
gcloud auth application-default login

# Create Firestore database (Terraform can't do this)
export GCP_PROJECT_ID=your-workspace-project-id
gcloud firestore databases create \
  --location=us-central1 \
  --type=firestore-native \
  --project=$GCP_PROJECT_ID

# Deploy infrastructure
terraform init
terraform plan
terraform apply

# Save outputs
terraform output -json > ../outputs.json
```

This creates:
- All required GCP APIs
- Service accounts with permissions
- Secret Manager secrets
- GCS bucket with lifecycle
- Cloud Run service
- Cloud Scheduler job

See [terraform/README.md](terraform/README.md) for details.

**Alternative**: Manual setup (legacy, not recommended):

```bash
# Set your project ID
export GCP_PROJECT_ID=your-project-id
gcloud config set project $GCP_PROJECT_ID

# Enable required APIs
gcloud services enable \
  firestore.googleapis.com \
  aiplatform.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  chat.googleapis.com \
  cloudscheduler.googleapis.com

# Create Firestore database
gcloud firestore databases create \
  --location=us-central1 \
  --type=firestore-native

# Initialize Firestore collections
python scripts/setup_firestore.py --project-id $GCP_PROJECT_ID
```

### 3. Export Existing Agent Configuration (Optional)

If you have an existing Vertex AI agent (e.g., Growth Coach):

```bash
# List agents to find your agent ID
gcloud ai agents list --location=us-central1

# Export agent configuration as template
gcloud ai agents describe AGENT_ID \
  --location=us-central1 \
  --format=yaml > vertex-ai-agent-config.yml
```

### 4. Slack App Setup

**IMPORTANT: Disable "Agent or Assistant" Mode**

After creating your Slack app, you must ensure it is NOT configured as an "Agent or Assistant":

1. Go to https://api.slack.com/apps → Your app
2. Navigate to **"Agents & AI Apps"** in the left sidebar
3. Ensure your app is **NOT** configured as an "Agent or Assistant"
4. If it is enabled, disable it

> **Why?** Slack's "Agent or Assistant" mode changes the DM UI to show each message exchange separately (like a search result) instead of as a continuous conversation thread. This is designed for one-off query assistants, not conversational bots.

**Option A: Export Existing Bot Manifest (Recommended)**

```bash
# Install Slack CLI
curl -fsSL https://downloads.slack-edge.com/slack-cli/install.sh | bash

# Login to Slack
slack login

# List your apps
slack apps list

# Export manifest from existing bot
slack apps manifest export <app-id> > slack-app-manifest.yml

# Save as template
cp slack-app-manifest.yml slack-app-manifest.template.yml
```

**Option B: Create New Bot from Scratch**

1. Go to https://api.slack.com/apps
2. Click "Create New App" → "From scratch"
3. Name your app (e.g., "Growth Coach")
4. Select your workspace
5. Navigate to "OAuth & Permissions"
6. Add Bot Token Scopes:
   - `chat:write`
   - `im:history`
   - `im:read`
7. Install to workspace
8. Copy "Bot User OAuth Token" (starts with `xoxb-`)
9. Go to "Basic Information"
10. Copy "Signing Secret"

### 5. Register Agent with Middleware

**IMPORTANT**: The `slack-bot-id` must be the **user_id** returned by Slack's auth.test API (starts with `U`), NOT the bot ID shown in Slack app settings (starts with `B`).

```bash
# First, get the correct bot user ID from your token
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer xoxb-your-token-here" | jq .user_id
# This returns something like "U0AFZ86NE00" - use THIS value for --slack-bot-id

# For Vertex AI Reasoning Engines, the agent ID format is:
# projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID

# Deploy your agent configuration to Firestore
python scripts/deploy_agent.py \
  --agent-name "Growth Coach" \
  --vertex-ai-agent-id "projects/PROJECT/locations/us-central1/reasoningEngines/1234567890" \
  --slack-bot-id "U0AFZ86NE00" \
  --slack-bot-token "xoxb-your-token-here" \
  --project-id $GCP_PROJECT_ID

# The script will:
# 1. Validate Vertex AI agent format
# 2. Validate Slack bot token and confirm the user_id matches
# 3. Create/update agent in Firestore
```

### 6. Local Development

```bash
# Install ngrok (if not already installed)
# Option A: Using package manager (requires sudo)
# sudo apt install ngrok  # or: brew install ngrok

# Option B: Direct download (no sudo required)
mkdir -p ~/bin
curl -s https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz | tar xz -C ~/bin
export PATH="$HOME/bin:$PATH"  # Add to ~/.bashrc for persistence

# Authenticate ngrok (get token from https://dashboard.ngrok.com/get-started/your-authtoken)
ngrok config add-authtoken YOUR_NGROK_TOKEN

# Terminal 1: Start ngrok tunnel
ngrok http 8080
# Copy the https URL (e.g., https://abc123.ngrok-free.dev)

# Terminal 2: Run FastAPI server
uvicorn app.main:app --reload --port 8080
```

### 7. Configure Slack Events API

1. Go to https://api.slack.com/apps → Your app
2. Navigate to "Event Subscriptions"
3. Enable Events
4. Set Request URL: `https://YOUR_NGROK_URL.ngrok.io/api/v1/slack/events`
5. Wait for green checkmark ✓ (URL verification)
6. Under "Subscribe to bot events", add: `message.im`
7. Save Changes
8. Reinstall app to workspace if prompted

### 8. Test

```bash
# Test health endpoint
curl http://localhost:8080/health

# Test Slack verification
curl -X POST http://localhost:8080/api/v1/slack/events \
  -H "Content-Type: application/json" \
  -d '{"type":"url_verification","challenge":"test123","token":"test"}'

# Expected: {"challenge":"test123"}

# Send a DM to your bot in Slack
# Check terminal logs for processing
```

## Production Deployment

### Deploy to Cloud Run

```bash
# Create secrets in Secret Manager
echo -n "your-slack-signing-secret" | gcloud secrets create slack-signing-secret \
  --data-file=- \
  --replication-policy="automatic"

# Grant Cloud Run access to secrets
PROJECT_NUMBER=$(gcloud projects describe $GCP_PROJECT_ID --format="value(projectNumber)")
gcloud secrets add-iam-policy-binding slack-signing-secret \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Deploy using Cloud Build
gcloud builds submit --config cloudbuild.yaml

# Or deploy directly
gcloud run deploy slack-vertex-middleware \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars GCP_PROJECT_ID=$GCP_PROJECT_ID,ENVIRONMENT=production \
  --set-secrets SLACK_SIGNING_SECRET=slack-signing-secret:latest

# Get Cloud Run URL
gcloud run services describe slack-vertex-middleware \
  --region us-central1 \
  --format 'value(status.url)'
```

### Update Slack Events API

1. Go to https://api.slack.com/apps → Your app → Event Subscriptions
2. Update Request URL to Cloud Run URL: `https://YOUR_CLOUD_RUN_URL/api/v1/slack/events`
3. Wait for verification ✓
4. Save Changes

## Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `GCP_PROJECT_ID` | Yes | Your GCP project ID | `my-project-123` |
| `GCP_LOCATION` | No | GCP location | `us-central1` (default) |
| `SLACK_SIGNING_SECRET` | Yes | Comma-separated Slack app signing secrets (one per bot) | `secret1,secret2` |
| `FIRESTORE_AGENTS_COLLECTION` | No | Firestore collection name | `agents` (default) |
| `FIRESTORE_SESSIONS_COLLECTION` | No | Firestore collection name | `sessions` (default) |
| `SESSION_TIMEOUT_MINUTES` | No | Session expiry (minutes of inactivity) | `30` (default) |
| `ENVIRONMENT` | No | Environment name | `development` / `production` |
| `LOG_LEVEL` | No | Logging level | `INFO` (default) |
| `FIRESTORE_EMULATOR_HOST` | No (local only) | Firestore emulator address | `localhost:8681` |

## Agent Deployment Workflow

When you deploy a new version of an agent to Vertex AI:

```bash
# 1. Deploy agent to Vertex AI (in your agent repo)
# For Reasoning Engines, deployment will output an ID like:
# projects/PROJECT/locations/us-central1/reasoningEngines/1234567890

# 2. Get the correct bot user_id (IMPORTANT: use user_id, not bot_id)
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" | jq .user_id
# Output: "U0AFZ86NE00"

# 3. Update middleware (in this repo)
python scripts/deploy_agent.py \
  --agent-name "Growth Coach" \
  --vertex-ai-agent-id "projects/PROJECT/locations/us-central1/reasoningEngines/NEW_ID" \
  --slack-bot-id "U0AFZ86NE00" \
  --slack-bot-token "$SLACK_BOT_TOKEN"

# 4. Test
# Send a DM to the bot in Slack
```

## Project Structure

```
slack_to_agent_integration/
├── app/
│   ├── main.py                 # FastAPI app + lifespan
│   ├── config.py               # Pydantic Settings
│   ├── api/v1/                 # API endpoints
│   │   ├── slack_events.py     # Slack Events API
│   │   └── routes.py           # Route aggregation
│   ├── services/               # Business logic
│   │   ├── firestore_service.py
│   │   ├── vertex_ai_service.py
│   │   ├── slack_service.py
│   │   └── message_processor.py
│   ├── models/                 # Data models
│   │   ├── agent.py
│   │   └── session.py
│   └── schemas/                # Pydantic schemas
│       └── slack.py
├── scripts/
│   ├── deploy_agent.py         # Agent deployment script
│   └── setup_firestore.py      # Firestore initialization
├── docs/                       # Detailed documentation
├── Dockerfile
├── cloudbuild.yaml
├── requirements.txt
├── .env.example
└── README.md
```

## Troubleshooting

### Bot doesn't respond to messages

- **Check Firestore**: Verify agent is registered with correct `slack_bot_id`
  ```bash
  gcloud firestore documents list --collection=agents
  ```
- **Check Slack Events**: Ensure Request URL is verified (green checkmark)
- **Check logs**:
  - Local: Terminal output
  - Production: `gcloud run logs read slack-vertex-middleware --region us-central1`

### "Agent not found" error

This is usually caused by incorrect `slack_bot_id` in Firestore.

**IMPORTANT**: Use the `user_id` from auth.test (starts with `U`), NOT the bot ID from Slack settings (starts with `B`):
```bash
# Get correct ID
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer xoxb-your-token" | jq .user_id
```

### "Slack verification failed" or 401 Unauthorized errors

**Symptoms**: Bot doesn't respond to messages, logs show "Invalid Slack signature" or "401 Unauthorized"

**Cause**: Each Slack app has its own signing secret. If you have multiple bots and haven't configured all their signing secrets, some bots will be rejected.

**Solution**:
1. Check logs to see which bot is failing:
   ```bash
   gcloud run logs read slack-vertex-middleware --region us-central1 --limit 50 | grep "401\|Invalid"
   ```
2. Ensure **all** Slack signing secrets are in your `.env` file (comma-separated):
   ```bash
   SLACK_SIGNING_SECRET=secret1,secret2,secret3
   ```
3. Get signing secrets from each Slack app:
   - Go to https://api.slack.com/apps → Your app → Basic Information
   - Copy "Signing Secret" under "App Credentials"
4. Redeploy after updating `.env`:
   ```bash
   ./scripts/deploy_middleware.sh
   ```

**Note**: The middleware verifies incoming webhook signatures against all configured signing secrets to support multiple Slack apps.

### "URL verification failed" (Slack)

- Ensure middleware is running before configuring Slack URL
- If adding a new bot, add its signing secret to `SLACK_SIGNING_SECRET` (comma-separated) **before** configuring the Event Subscriptions URL
- For ngrok: Make sure tunnel is active and URL is correct
- Check logs for signature verification errors

### pip install takes forever / dependency errors

The google-cloud-aiplatform package has many dependencies. Use the pinned versions in requirements.txt:
```bash
pip install -r requirements.txt
```
If you still have issues, try:
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### "ModuleNotFoundError: No module named 'aiohttp'"

This is required for async Slack SDK. It should be in requirements.txt, but if missing:
```bash
pip install aiohttp
```

## Security Notes

- **Never commit `.env`** - It's in `.gitignore`
- **Never commit service account keys** (*.json files)
- **Use Secret Manager** for production secrets
- **Rotate tokens** if exposed
- **Review permissions** regularly

## Adding a New Platform

The middleware's platform abstraction makes it straightforward to add new messaging platforms (WhatsApp, Discord, Microsoft Teams, etc.). Telegram serves as the reference implementation for this process.

### Architecture Overview

```
Platform Webhook
  ↓
parse_event() → PlatformEvent (unified schema)
  ↓
MessageProcessor (platform-agnostic)
  ↓
Vertex AI Agent
  ↓
connector.send_message() → Platform
```

**Key Design Principles:**
- **Platform-agnostic core**: All business logic lives in `MessageProcessorV2`
- **Unified identity**: Users maintain the same identity across all platforms
- **Session continuity**: Conversations persist across platforms
- **Modular secrets**: Each agent's platform credentials stored separately

### Implementation Checklist

Using Telegram as the reference implementation:

**1. Create Platform Connector** ([app/services/platforms/telegram_connector.py](app/services/platforms/telegram_connector.py))
- [ ] Implement `PlatformConnector` interface from [base.py](app/services/platforms/base.py)
- [ ] Implement 6 required methods:
  - `verify_request()` - Authenticate webhook requests
  - `parse_event()` - Transform platform events → `PlatformEvent` ([platform_event.py](app/schemas/platform_event.py))
  - `send_message()` - Send responses back to users
  - `download_file()` - Download file attachments
  - `get_user_info()` - Fetch user profile data
  - `open_conversation()` - Get/create DM conversation ID
- [ ] Support both direct tokens and Secret Manager credentials

**2. Create Route Handler** ([app/api/v1/telegram_events.py](app/api/v1/telegram_events.py))
- [ ] Create FastAPI router with `POST /{platform}/events` endpoint
- [ ] Handle platform-specific event types (messages, edits, etc.)
- [ ] Filter bot messages to prevent loops
- [ ] Identify agent from platform config
- [ ] Verify request authenticity
- [ ] Process event in background task

**3. Register Router** ([app/api/v1/routes.py](app/api/v1/routes.py))
- [ ] Import platform router
- [ ] Include router in main API router

**4. Extend Agent Model** ([app/models/agent.py](app/models/agent.py))
- [ ] Add platform-specific fields to `AgentPlatformConfig`
- [ ] Add convenience method `get_{platform}_config()`
- [ ] Update platform field description to include new platform

**5. Add Terraform Secret Template** ([docs/terraform-templates/agent-project/main.tf](docs/terraform-templates/agent-project/main.tf))
- [ ] Add commented section for platform-specific secrets
- [ ] Include instructions for token storage
- [ ] Add IAM binding instructions for middleware access

**6. Update Documentation**
- [ ] Update README.md features and architecture
- [ ] Add platform setup guide to [FOR_AGENT_DEVELOPERS.md](docs/FOR_AGENT_DEVELOPERS.md)
- [ ] Add troubleshooting section
- [ ] Document credential creation and configuration

**7. Create Identity Linking Support**
- [ ] Test with [scripts/link_identities.py](scripts/link_identities.py) (already supports any platform!)
- [ ] Verify cross-platform session continuity

**8. End-to-End Testing**
- [ ] Create test bot on the platform
- [ ] Deploy middleware changes
- [ ] Configure webhook
- [ ] Send test message
- [ ] Verify identity resolution, session creation, and Vertex AI routing

### Reference Files

**Core Abstractions:**
- [app/services/platforms/base.py](app/services/platforms/base.py) - `PlatformConnector` interface definition
- [app/schemas/platform_event.py](app/schemas/platform_event.py) - Unified event schema
- [app/services/message_processor_v2.py](app/services/message_processor_v2.py) - Platform-agnostic processor

**Reference Implementation (Telegram):**
- [app/services/platforms/telegram_connector.py](app/services/platforms/telegram_connector.py) - Complete connector
- [app/api/v1/telegram_events.py](app/api/v1/telegram_events.py) - Route handler
- [app/models/agent.py](app/models/agent.py) - See `telegram_*` fields in `AgentPlatformConfig`

**Other Platforms for Comparison:**
- [app/services/platforms/slack_connector.py](app/services/platforms/slack_connector.py) - HMAC signature verification
- [app/services/platforms/google_chat_connector.py](app/services/platforms/google_chat_connector.py) - Service account auth

### Benefits of This Architecture

1. **Minimal Code**: ~300 lines to add a complete platform integration
2. **Automatic Features**: New platforms get identity management, sessions, and scheduled jobs for free
3. **Cross-Platform Users**: Users auto-link via email, maintaining conversations across all platforms
4. **Consistent Experience**: Same agent behavior regardless of platform
5. **Isolated Concerns**: Platform bugs don't affect other platforms or core logic

### Future Platform Ideas

- **WhatsApp Business API** - Enterprise messaging
- **Discord** - Community and gaming
- **Microsoft Teams** - Enterprise collaboration
- **Line** - Popular in Asia
- **Facebook Messenger** - Social integration
- **SMS via Twilio** - Universal accessibility

## Documentation

### Setup & Infrastructure
- **[Terraform README](terraform/README.md)** - Middleware infrastructure deployment
- **[Terraform Templates](docs/terraform-templates/)** - Templates for agent-specific infrastructure
  - [Agent Project Template](docs/terraform-templates/agent-project/) - Dedicated GCP project for agents requiring separate projects
- [GCP Setup Guide](docs/GCP_SETUP.md) - GCP project configuration

### Platform Integration
- [Slack Setup Guide](docs/SLACK_SETUP.md) - Detailed Slack app creation
- **[For Agent Developers](docs/FOR_AGENT_DEVELOPERS.md)** - Complete guide for deploying agents (Slack + Google Chat + MCP)
  - Copy this to your agent repository for easy reference

### MCP Integration
- **[Using the MCP Server](docs/USING_MCP_SERVER.md)** - Global/owner MCP endpoint: adding servers, building custom ones, connecting Claude Code

### Development & Operations
- [Agent Deployment](docs/AGENT_DEPLOYMENT.md) - How to deploy/update agents
- [Troubleshooting](docs/TROUBLESHOOTING.md) - Common issues

## License

[Your License]

## Support

For issues or questions, please open an issue on GitHub.
