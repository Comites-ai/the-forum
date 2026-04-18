# Terraform Infrastructure for Vertex AI Middleware

This directory contains Terraform configuration for deploying the complete GCP infrastructure for the Slack/Google Chat Vertex AI middleware.

## What Gets Created

- **APIs**: All required GCP APIs (Firestore, Vertex AI, Cloud Run, Secret Manager, etc.)
- **Service Accounts**:
  - `scheduler-sa` (Cloud Scheduler invoker)
  - Default Compute SA (used by Cloud Run with necessary permissions)
- **IAM Permissions**: All necessary roles and permissions for middleware
- **Secret Manager**: Slack signing secret placeholder + `mcp-global-api-key` secret (for the global MCP endpoint)
- **GCS Bucket**: Temporary storage for Slack file uploads (1-day lifecycle)
- **Cloud Run**: Middleware service deployment
- **Cloud Scheduler**: Scheduled job dispatcher (runs every minute)

**Note**: Agent-specific infrastructure (like Google Chat bot service accounts) should be created in separate terraform configurations. See [../docs/terraform-templates/](../docs/terraform-templates/) for templates.

## Prerequisites

1. **Google Workspace Business Starter** account (for Google Chat bot support)
2. **GCP Project** created in Workspace organization
3. **Terraform** installed (v1.0+)
4. **gcloud CLI** installed and authenticated
5. **Project Owner** or **Editor** permissions

## Initial Setup

### 1. Configure Terraform Variables

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:
```hcl
project_id  = "your-workspace-project-id"
region      = "us-central1"
environment = "prod"
```

### 2. Authenticate with GCP

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### 3. Create Firestore Database

Terraform cannot create Firestore databases, so create it manually:

```bash
gcloud firestore databases create \
  --location=us-central1 \
  --type=firestore-native \
  --project=YOUR_PROJECT_ID
```

### 4. (Optional) Setup Terraform State Backend

For team collaboration, store Terraform state in GCS:

```bash
# Create bucket for Terraform state
gsutil mb gs://YOUR_PROJECT_ID-terraform-state

# Enable versioning
gsutil versioning set on gs://YOUR_PROJECT_ID-terraform-state
```

Uncomment the backend configuration in `providers.tf`:
```hcl
backend "gcs" {
  bucket = "YOUR_PROJECT_ID-terraform-state"
  prefix = "middleware/state"
}
```

## Deployment

### 1. Initialize Terraform

```bash
terraform init
```

### 2. Review the Plan

```bash
terraform plan
```

Review all resources that will be created. Verify:
- Correct project ID
- All required APIs
- Service accounts and permissions
- GCS bucket name

### 3. Apply Infrastructure

```bash
terraform apply
```

Type `yes` when prompted. This will take 5-10 minutes.

### 4. Save Outputs

```bash
terraform output -json > outputs.json
```

The outputs include:
- Cloud Run URL
- Service account emails
- GCS bucket name
- Webhook URLs (Slack, Google Chat)
- Global MCP endpoint URL (`mcp_global_endpoint`)
- MCP API key secret name (`mcp_global_api_key_secret`)
- Next steps guide

## Post-Deployment Steps

After `terraform apply` completes, you must:

### 1. Add Slack Signing Secret(s)

```bash
PROJECT_ID=$(terraform output -raw project_id)

# Add your Slack app signing secret(s) - comma-separated if multiple
echo -n "YOUR_SLACK_SECRET" | gcloud secrets versions add slack-signing-secret \
  --data-file=- \
  --project=$PROJECT_ID

# If you have multiple Slack apps:
# echo -n "secret1,secret2,secret3" | gcloud secrets versions add slack-signing-secret \
#   --data-file=- \
#   --project=$PROJECT_ID
```

### 2. Configure Agent-Specific Infrastructure

For each agent that uses Google Chat:
1. See [../docs/terraform-templates/agent-project/](../docs/terraform-templates/agent-project/) for terraform templates
2. Follow [../docs/FOR_AGENT_DEVELOPERS.md](../docs/FOR_AGENT_DEVELOPERS.md) for complete setup instructions

### 3. Deploy Cloud Run Middleware

```bash
cd ..
gcloud builds submit --config cloudbuild.yaml --project $PROJECT_ID
```

## Updating Infrastructure

To modify infrastructure:

1. Edit the relevant `.tf` file
2. Run `terraform plan` to review changes
3. Run `terraform apply` to apply changes

## Common Operations

### View Current State

```bash
terraform show
```

### List All Resources

```bash
terraform state list
```

### Get Specific Output

```bash
terraform output cloud_run_url
terraform output slack_webhook_url
terraform output google_chat_webhook_url
```

### Refresh State

```bash
terraform refresh
```

## Destroying Infrastructure

**WARNING**: This will delete all resources. Ensure you have backups!

```bash
terraform destroy
```

## Troubleshooting

### API Not Enabled Error

If you see "API not enabled" errors, wait a few minutes for APIs to propagate, then run `terraform apply` again.

### Permission Denied Errors

Ensure you have the following roles:
- `roles/owner` or `roles/editor` on the project
- `roles/iam.securityAdmin` (for service account creation)
- `roles/resourcemanager.projectIamAdmin` (for IAM bindings)

### Firestore Error

Terraform cannot create Firestore databases. Create it manually first:
```bash
gcloud firestore databases create --location=us-central1 --type=firestore-native --project=YOUR_PROJECT_ID
```

### Cloud Run Image Not Found

The initial Cloud Run deployment uses a placeholder image. Deploy the actual application using Cloud Build:
```bash
cd ..
gcloud builds submit --config cloudbuild.yaml --project YOUR_PROJECT_ID
```

## File Structure

```
terraform/
├── README.md                 # This file
├── terraform.tfvars.example  # Example variables
├── terraform.tfvars          # Your variables (gitignored)
├── providers.tf              # Terraform and provider config
├── variables.tf              # Variable definitions
├── apis.tf                   # GCP API enablement
├── firestore.tf              # Firestore placeholder
├── service_accounts.tf       # Service accounts and IAM
├── secrets.tf                # Secret Manager secrets
├── storage.tf                # GCS bucket configuration
├── cloud_run.tf              # Cloud Run service
├── scheduler.tf              # Cloud Scheduler job
└── outputs.tf                # Output values
```

## Next Steps

After Terraform deployment:
1. Deploy Cloud Run middleware (see Post-Deployment Steps above)
2. Deploy your Vertex AI agents
3. Register agents with middleware using `scripts/deploy_agent.py`
4. For Google Chat bots: follow agent-specific terraform setup (see ../docs/FOR_AGENT_DEVELOPERS.md)
5. Configure Slack Event Subscriptions with webhook URLs
6. (Optional) Enable the global MCP endpoint for Claude Code:
   - Populate the `mcp-global-api-key` secret and set `MCP_GLOBAL_API_KEY_SECRET` env var
   - See [../docs/USING_MCP_SERVER.md](../docs/USING_MCP_SERVER.md) for step-by-step instructions
7. Test all integrations

See ../docs/FOR_AGENT_DEVELOPERS.md for complete agent deployment guide.
