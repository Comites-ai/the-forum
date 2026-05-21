# Terraform Infrastructure for The Forum

This directory contains Terraform configuration for deploying the complete GCP infrastructure for The Forum by Comites.ai.

For most users, [`../scripts/install.sh`](../scripts/install.sh) is the easiest way to apply this terraform — it handles tfvars/.env generation, GCS state backend setup, terraform apply, Slack secret population, image deploy, and platform webhook setup as a single guided flow. The sections below cover the manual path and the variables you can tune.

## What Gets Created

- **APIs**: All required GCP APIs (Firestore, Vertex AI, Cloud Run, Secret Manager, Cloud Scheduler, Cloud Build, Cloud Storage, Cloud Trace, Logging, Chat).
- **Firestore**: The `(default)` database in your chosen region.
- **Service Accounts**:
  - `scheduler-sa` (Cloud Scheduler invoker)
  - IAM bindings on the default compute SA (Firestore, Vertex AI, Cloud Run, Logging, Cloud Storage, Service Account User, Artifact Registry writer)
- **Secret Manager**: terraform creates only the secret *containers* (and their IAM bindings); secret values are populated out-of-band via `gcloud secrets versions add` and are never stored in terraform state. Containers created: `slack-signing-secret` when `var.use_slack = true`, `discord-bot-token` when `var.use_discord = true`, and `oauth-client-id` / `oauth-client-secret` / `admin-session-secret` when `var.enable_admin_ui = true`. See Post-Deployment Steps for population commands.
- **GCS Buckets**:
  - `${PROJECT_ID}-slack-files` — temporary storage for uploaded Slack files (1-day lifecycle).
  - `${PROJECT_ID}-staging` — staging bucket for ADK Agent Engine deploys (7-day lifecycle, force_destroy=false).
- **Cloud Run**: The Forum service. Initial revision uses a public hello-world placeholder image (`us-docker.pkg.dev/cloudrun/container/hello`); `scripts/deploy_forum.sh` swaps in the real image via Cloud Build.
- **Cloud Scheduler**: `scheduled-jobs-dispatcher` — invokes Cloud Run every minute.

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
environment = "production"

# Set to false to skip Slack-related infrastructure (secret container,
# IAM binding, Cloud Run env binding). Default is true.
use_slack   = true
```

### Available variables

| Variable | Default | Purpose |
|---|---|---|
| `project_id` | (required) | GCP project ID. |
| `region` | `us-central1` | Region for Cloud Run, GCS buckets, Firestore, scheduler. |
| `environment` | `prod` | Free-form label propagated to the Cloud Run `ENVIRONMENT` env var. |
| `gcs_bucket_lifecycle_days` | `1` | TTL for the slack-files bucket. |
| `cloud_run_service_name` | `the-forum` | Cloud Run service name. |
| `scheduler_job_name` | `scheduled-jobs-dispatcher` | Cloud Scheduler job name. |
| `scheduler_cron_schedule` | `* * * * *` | Cron schedule for the scheduler job. |
| `use_slack` | `true` | Whether to create the Slack signing secret container, its IAM binding, and the Cloud Run env binding. Set to `false` for non-Slack installs — `scripts/deploy_forum.sh` will auto-detect the absent secret and skip the Cloud Run binding. |
| `use_discord` | `false` | Whether to provision the discord-worker e2-micro VM, the `discord-bot-token` secret container, and the worker's IAM bindings. See [../docs/DISCORD_WORKER.md](../docs/DISCORD_WORKER.md) for cost and patching notes. |

### 2. Authenticate with GCP

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### 3. Bootstrap APIs

Terraform needs the Service Usage and Cloud Resource Manager APIs enabled before it can enable the rest. Run once per project:

```bash
gcloud services enable \
  serviceusage.googleapis.com \
  cloudresourcemanager.googleapis.com \
  --project=YOUR_PROJECT_ID
```

### 4. (Optional) Setup Terraform State Backend

For team collaboration, store Terraform state in GCS. The `install.sh` script does this automatically; for manual setup:

```bash
gcloud storage buckets create gs://YOUR_PROJECT_ID-terraform-state \
  --project=YOUR_PROJECT_ID \
  --location=us-central1 \
  --uniform-bucket-level-access \
  --public-access-prevention
gcloud storage buckets update gs://YOUR_PROJECT_ID-terraform-state --versioning
```

Uncomment the backend configuration in `providers.tf`:
```hcl
backend "gcs" {
  bucket = "YOUR_PROJECT_ID-terraform-state"
  prefix = "the-forum/state"
}
```

See [providers.tf.example](providers.tf.example) for the full documented template, including what `scripts/install.sh` writes for you and how to recreate it manually. The local providers.tf with your real bucket name is operator-specific — don't commit it.

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
- Next steps guide

## Post-Deployment Steps

After `terraform apply` completes:

### 1. Populate Secret Values

Terraform creates the Secret Manager *containers* (e.g. `slack-signing-secret`, `discord-bot-token`, the three admin UI secrets) but does **not** manage their values. Values are populated out-of-band — they never enter terraform state.

`scripts/install.sh` does this automatically (it prompts you and pipes the value through `gcloud secrets versions add`). If you're running terraform manually, populate each container yourself:

```bash
# Slack — single app
echo -n "YOUR_SLACK_SIGNING_SECRET" | gcloud secrets versions add slack-signing-secret \
  --data-file=- --project="$PROJECT_ID"

# Slack — multiple apps, one signing secret per bot, comma-separated
echo -n "secret1,secret2,secret3" | gcloud secrets versions add slack-signing-secret \
  --data-file=- --project="$PROJECT_ID"

# Discord
echo -n "YOUR_DISCORD_BOT_TOKEN" | gcloud secrets versions add discord-bot-token \
  --data-file=- --project="$PROJECT_ID"

# Admin UI (only if enable_admin_ui = true)
echo -n "$OAUTH_CLIENT_ID" | gcloud secrets versions add oauth-client-id \
  --data-file=- --project="$PROJECT_ID"
echo -n "$OAUTH_CLIENT_SECRET" | gcloud secrets versions add oauth-client-secret \
  --data-file=- --project="$PROJECT_ID"
openssl rand -hex 32 | gcloud secrets versions add admin-session-secret \
  --data-file=- --project="$PROJECT_ID"
```

To **rotate** any of these later: just run `gcloud secrets versions add` again. Cloud Run binds env vars to `:latest`, so the new version takes effect on the next Cloud Run revision (i.e. next `scripts/deploy_forum.sh`).

If you set `use_slack = false`, terraform won't create the container and `scripts/deploy_forum.sh` will skip the Cloud Run binding automatically.

### 2. Configure Agent-Specific Infrastructure

For each agent that uses Google Chat:
1. See [../docs/terraform-templates/agent-project/](../docs/terraform-templates/agent-project/) for terraform templates
2. Follow [../docs/FOR_AGENT_DEVELOPERS.md](../docs/FOR_AGENT_DEVELOPERS.md) for complete setup instructions

### 3. Deploy The Forum to Cloud Run

```bash
cd ..
./scripts/deploy_forum.sh
```

This builds the image via Cloud Build and rolls it out, replacing the hello-world placeholder Cloud Run revision created by terraform.

### 4. Admin UI (optional)

To turn on the operator-facing admin UI at `/admin`, set `enable_admin_ui = true` and `terraform apply`. Terraform creates the three secret containers (`oauth-client-id`, `oauth-client-secret`, `admin-session-secret`) but does not populate them. Populate them yourself (see step 1) or let `scripts/install.sh` handle it.

After the apply completes, terraform outputs `admin_redirect_uri`. Register it on the OAuth client (GCP Console → APIs & Services → Credentials), then push it onto the Cloud Run service — this step can't be done from inside the same terraform apply because the Cloud Run URL is only known after the service is created:

```bash
ADMIN_REDIRECT_URI=$(terraform output -raw admin_redirect_uri)
gcloud run services update the-forum \
  --region "$(terraform output -raw region)" \
  --update-env-vars "OAUTH_REDIRECT_URI=${ADMIN_REDIRECT_URI}"
```

Until this last step the admin UI stays cleanly disabled (`settings.admin_ui_enabled` is False and `/admin/*` returns 404). See [../docs/ADMIN_UI.md](../docs/ADMIN_UI.md) for the full bring-up.

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

**Recommended**: use the guided uninstaller, which backs up secrets and Firestore data to `./migration-data/` before destroying:

```bash
./scripts/uninstall.sh
```

It empties the staging bucket (terraform can't auto-empty it), disables Firestore delete protection, runs `terraform destroy`, and asks before deleting container images and the state bucket.

**Manual** (advanced):

```bash
# 1. Empty the staging bucket (terraform's force_destroy=false)
gcloud storage rm --recursive gs://YOUR_PROJECT_ID-staging/** --quiet

# 2. Disable Firestore delete protection
gcloud firestore databases update --database='(default)' --project=YOUR_PROJECT_ID --no-delete-protection

# 3. Destroy
terraform destroy
```

Either way, ensure you have backups of any data you care about (Firestore collections, secret values).

## Troubleshooting

### API Not Enabled Error

If you see "API not enabled" errors, wait a few minutes for APIs to propagate, then run `terraform apply` again.

### Permission Denied Errors

Ensure you have the following roles:
- `roles/owner` or `roles/editor` on the project
- `roles/iam.securityAdmin` (for service account creation)
- `roles/resourcemanager.projectIamAdmin` (for IAM bindings)

### Cloud Run service starts but shows "Hello, world"

That's the public placeholder image (`us-docker.pkg.dev/cloudrun/container/hello`) that terraform creates the Cloud Run service with so the first apply succeeds before any real image has been pushed. Deploy the actual application:

```bash
cd ..
./scripts/deploy_forum.sh
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
1. Deploy The Forum to Cloud Run (see Post-Deployment Steps above)
2. Deploy your Vertex AI agents
3. Register agents with The Forum using `scripts/deploy_agent.py`
4. For Google Chat bots: follow agent-specific terraform setup (see ../docs/FOR_AGENT_DEVELOPERS.md)
5. Configure Slack Event Subscriptions with webhook URLs
6. Test all integrations

See ../docs/FOR_AGENT_DEVELOPERS.md for complete agent deployment guide.
