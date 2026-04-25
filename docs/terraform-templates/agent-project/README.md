# Agent Infrastructure - Terraform Template

This Terraform template creates dedicated GCP infrastructure for agents that require it.

## Do You Need This?

**Use this template if your agent:**
- ✅ Uses Google Chat (REQUIRED - Google Chat API restriction)
- ✅ Needs dedicated service account for Google APIs (Drive, Sheets, etc.)
- ✅ Requires organizational separation from middleware

**Skip this template if your agent:**
- ❌ Only uses Slack and/or Telegram (no dedicated infrastructure needed)
- ❌ Doesn't access Google APIs

**Note for Slack/Telegram only agents:**
- Slack: Create bot in Slack UI, store token in middleware's Secret Manager
- Telegram: Create bot via @BotFather, store token in middleware's Secret Manager
- No dedicated GCP project required!

## What This Creates

For agents that need dedicated infrastructure, this creates:
- New GCP project (required for Google Chat bots)
- Service account with appropriate permissions
- Required API enablements (Chat, Drive, Sheets, etc.)
- Organization policy override for service account key creation

## What This Creates

For agents that need dedicated infrastructure:
- New GCP project
- Required APIs enabled (customizable per agent)
- A single service account for the agent — used for both Google APIs (Drive/Sheets/Docs) and, when Google Chat is enabled, signing Chat messages
- Organization policy override to allow service account key creation
- Staging bucket for ADK deployments
- Secrets for platform credentials (Slack, Telegram, Google Chat)
- Outputs with next steps for configuration

## Slack/Telegram-Only Agents

If your agent **only uses Slack and/or Telegram** and doesn't need Google APIs:

**You don't need this template!** Simply:
1. **Slack**: Create bot in Slack UI → store token in middleware's Secret Manager
2. **Telegram**: Create bot via @BotFather → store token in middleware's Secret Manager
3. Register your agent with the middleware using `scripts/deploy_agent.py`

No dedicated GCP infrastructure required for Slack/Telegram!

## Prerequisites

For agents that DO need dedicated infrastructure:

- GCP organization ID (Google Chat bots require a Workspace organization)
- Billing account ID
- Terraform 1.0+
- `gcloud` CLI authenticated

## Usage

### 1. Copy Template to Your Agent Repository

```bash
# In your agent repository
mkdir -p terraform
cp -r /path/to/middleware/docs/terraform-templates/agent-project/* terraform/
cd terraform
```

### 2. Configure Variables

```bash
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars
```

Fill in your values:
- `project_id`: Globally unique ID for the new project
- `organization_id`: Your GCP organization ID
- `billing_account`: Your billing account ID
- `agent_name`: Display name for your agent
- `agent_account_id`: Service account name (lowercase, hyphens)
- `secret_name`: Name for Secret Manager secret in middleware project

### 3. Deploy

```bash
terraform init
terraform plan
terraform apply
```

### 4. Follow Next Steps

After `terraform apply` completes, follow the "next_steps" output instructions:

#### 4a. Create Service Account Key (for Google Chat)

```bash
# Get the service account email from terraform output
export AGENT_SA_EMAIL=$(terraform output -raw service_account_email)
export PROJECT_ID=$(terraform output -raw project_id)

# Create the key (this same SA is also what your spreadsheets are shared with)
gcloud iam service-accounts keys create ${PROJECT_ID}-sa-key.json \
  --iam-account=$AGENT_SA_EMAIL \
  --project=$PROJECT_ID
```

#### 4b. Store Key in Secret Manager

**IMPORTANT**: Store the key in **your agent's project** (not the middleware project):

```bash
# Store in YOUR agent's project's Secret Manager
gcloud secrets versions add your-agent-credentials \
  --data-file=${PROJECT_ID}-sa-key.json \
  --project=$PROJECT_ID

# Securely delete the key file
rm -f ${PROJECT_ID}-sa-key.json
```

#### 4c. Store Slack Token (if using Slack)

If your agent uses Slack, store the token in your agent's project:

```bash
# Get your Slack bot token from https://api.slack.com/apps
echo -n "xoxb-YOUR-SLACK-BOT-TOKEN" | gcloud secrets versions add your-agent-slack-token \
  --data-file=- \
  --project=$PROJECT_ID
```

#### 4d. Grant Middleware Access to Secrets

**CRITICAL STEP**: The middleware needs permission to read secrets from your agent's project. Without this, all messages will fail with `403 Permission Denied` errors.

```bash
# Set up variables
export MIDDLEWARE_PROJECT_ID="vertex-ai-middleware-prod"
export MIDDLEWARE_PROJECT_NUMBER=$(gcloud projects describe $MIDDLEWARE_PROJECT_ID --format="value(projectNumber)")
export MIDDLEWARE_SA="${MIDDLEWARE_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Grant access to Google Chat credentials
gcloud secrets add-iam-policy-binding your-agent-credentials \
  --member="serviceAccount:${MIDDLEWARE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT_ID

# Grant access to Slack token (if using Slack)
gcloud secrets add-iam-policy-binding your-agent-slack-token \
  --member="serviceAccount:${MIDDLEWARE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT_ID
```

**What this does**: Allows the middleware's Cloud Run service account to read your bot credentials so it can authenticate API calls to Google Chat and/or Slack.

#### 4e. Create Staging Bucket for ADK Deployments

ADK requires a Cloud Storage bucket to stage deployment artifacts when deploying your agent to Vertex AI.

```bash
export PROJECT_ID=$(terraform output -raw project_id)

# Create staging bucket (one-time setup)
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
```

**Note**: If the bucket doesn't exist, ADK will create it automatically on first deployment, but creating it manually allows you to set lifecycle policies and choose the location.

#### 4f. Deploy Your Agent to Vertex AI

Now deploy your agent code to Vertex AI Agent Engine:

```bash
# Navigate to your agent repository
cd /path/to/your-agent

# Deploy using ADK
# The staging bucket is used to upload your agent code before deployment
adk deploy agent_engine \
  --project "$PROJECT_ID" \
  --region us-central1 \
  --staging_bucket "gs://${PROJECT_ID}-staging" \
  --display_name "Your Agent Name" \
  --trace_to_cloud \
  your-agent-directory

# Note the Reasoning Engine ID from the output
# Format: projects/YOUR_PROJECT/locations/us-central1/reasoningEngines/ENGINE_ID
export AGENT_VERTEX_ID="projects/YOUR_PROJECT/locations/us-central1/reasoningEngines/YOUR_ENGINE_ID"
```

#### 4g. Configure Platform Settings

- For Google Chat: Configure the bot in Google Cloud Console
- For Slack: Configure the bot in Slack UI

#### 4h. Enable Platform in Firestore

Use the middleware scripts to register your agent and enable platforms:

```bash
cd /path/to/slack-vertex-ai-middleware

# Register the agent
python scripts/deploy_agent.py \
  --agent-name "Your Agent Name" \
  --vertex-ai-agent-id "projects/YOUR_PROJECT/locations/us-central1/reasoningEngines/YOUR_ENGINE_ID"

# Enable Google Chat (if using)
python scripts/enable_google_chat_agent.py \
  --project vertex-ai-middleware-prod \
  --agent-id "YOUR_AGENT_FIRESTORE_ID" \
  --secret-name "your-agent-credentials" \
  --google-chat-project-id "$PROJECT_ID"

# Enable Slack (if using)
python scripts/enable_slack_agent.py \
  --project vertex-ai-middleware-prod \
  --agent-id "YOUR_AGENT_FIRESTORE_ID" \
  --secret-name "your-agent-slack-token" \
  --slack-project-id "$PROJECT_ID"
```

**Note**: Use the `AGENT_VERTEX_ID` from step 4f when registering your agent.

#### 4i. Test the Agent

Send a test message through your configured platform(s).

## Section 5: Custom MCP Server (Optional)

The Terraform template includes a commented-out **Section 5** that provisions Cloud Run infrastructure for a custom MCP server — useful if your agent wants to expose its own tools to other agents or to the middleware owner's global endpoint.

**What Section 5 creates (when uncommented):**

| Resource | Description |
|----------|-------------|
| `google_cloud_run_v2_service.mcp_server` | Cloud Run service running your MCP server image |
| `google_cloud_run_v2_service_iam_member.mcp_public` | Public invoke access (auth handled by API key) |
| `google_secret_manager_secret.mcp_api_key` | API key secret for middleware → MCP server auth |
| `google_secret_manager_secret_iam_member.mcp_key_accessor` | Grants the middleware SA access to that secret |

**When to use this:**
- Your ADK agent wants to expose custom tools (e.g., proprietary APIs, internal databases)
- You want those tools available via the middleware's MCP proxy (`/api/v1/mcp/{agent_id}/sse`)
- You want to share the tools with the global owner endpoint

**How to enable:**
1. Uncomment Section 5 in `main.tf`
2. Build and push your MCP server image:
   ```bash
   gcloud builds submit \
     --tag gcr.io/${PROJECT_ID}/${BOT_ACCOUNT_ID}-mcp:latest \
     /path/to/your/mcp-server
   ```
3. Run `terraform apply`
4. Generate and store an MCP API key:
   ```bash
   openssl rand -base64 32 | tr -d '\n' | \
     gcloud secrets versions add ${BOT_ACCOUNT_ID}-mcp-api-key \
       --data-file=- --project=$PROJECT_ID
   ```
5. Register in your agent's Firestore document under `mcp_servers`:
   ```json
   {
     "name": "my-tools",
     "url": "https://{mcp-cloud-run-url}/sse",
     "enabled": true,
     "api_key_secret": "{bot_account_id}-mcp-api-key",
     "api_key_project_id": "{project_id}"
   }
   ```
6. Configure your ADK agent with `MCPToolset`:
   ```python
   MCPToolset(
       connection_params=SseServerParams(
           url="{middleware_url}/api/v1/mcp/{agent_id}/sse"
       )
   )
   ```

See [USING_MCP_SERVER.md](../USING_MCP_SERVER.md) for full details on building custom MCP servers and connecting Claude Code.

---

## Important Notes

### One Service Account for Everything

This template creates a single service account, `your-agent@PROJECT.iam.gserviceaccount.com`, that does double duty:

- **Google APIs (Drive, Sheets, Docs)** — share your spreadsheets and docs with this SA's email so the agent can read/write them.
- **Google Chat (when Section 3 is enabled)** — `roles/chat.owner` is granted to the same SA, and its key is stored in Secret Manager so the middleware can sign outbound Chat messages on the bot's behalf.

One SA, one key, one email to share files with. (Earlier versions of this template provisioned two SAs — a `*-apis` SA for Drive/Sheets and a separate chat-bot SA. We collapsed them after realizing the split added confusion without meaningful isolation: both SAs lived in the same project with similar permissions, and the chat-bot SA's key was already in Secret Manager.)

### Secret Location

Secrets are stored in **your agent's project**, not the middleware project. The middleware service account is granted `secretAccessor` permission to read them.

This approach:
- ✅ Better separation of concerns
- ✅ Easier to manage agent-specific credentials
- ✅ Cleaner project organization

### Common Issues

**"403 Permission Denied" when testing**: This is the most common error. You forgot step 4d (granting middleware access to secrets). Go back and run those commands.

**Messages not reaching agent**: Check the middleware logs:
```bash
gcloud run services logs read slack-vertex-middleware --project=vertex-ai-middleware-prod --region=us-central1 --limit=50
```

### Security & Cleanup

- **Security**: The service account key is sensitive. Delete it after storing in Secret Manager.
- **Organization Policy**: This template overrides the key creation policy for this project only.
- **Cleanup**: If you delete this project, you'll need to recreate it to re-enable the agent.

## Example Configuration

See `terraform.tfvars.example` for a complete example configuration.
