#!/usr/bin/env bash
#
# install.sh — Guided install of The Forum into a GCP project.
#
# This script walks through every step needed to stand up The Forum:
# auth, API bootstrapping, terraform.tfvars + .env generation, GCS state
# backend, terraform apply, secret population, optional Firestore restore
# from a migration backup, image build/deploy via Cloud Build, and
# verification. Re-running is safe: each phase detects existing state and
# offers to skip or overwrite.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- Output helpers ---
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
BOLD=$'\033[1m'
NC=$'\033[0m'

say()  { printf "%s==>%s %s\n" "$BLUE" "$NC" "$*"; }
ok()   { printf "%sOK%s  %s\n" "$GREEN" "$NC" "$*"; }
warn() { printf "%s!! %s%s\n" "$YELLOW" "$NC" "$*"; }
err()  { printf "%sxx%s  %s\n" "$RED" "$NC" "$*" >&2; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }

prompt_yn() {
    local prompt="$1"
    local default="${2:-n}"
    local hint="[y/N]"
    [[ "$default" == "y" ]] && hint="[Y/n]"
    local yn
    while true; do
        read -rp "$prompt $hint " yn
        yn="${yn:-$default}"
        case "$yn" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
            *) echo "Please answer y or n." ;;
        esac
    done
}

# --- Phase 1: Announce ---
phase_1_announce() {
    cat <<EOF
${BOLD}=== The Forum — Guided Install ===${NC}

This script walks through every step needed to install The Forum into a
GCP project. It will:

  2.  Verify gcloud CLI, authenticate, and select your GCP project.
  3.  Bootstrap-enable the APIs terraform itself needs.
  4.  Verify terraform CLI is installed.
  5.  Ask whether this is a migration from an existing install.
  6.  Ask which messaging platforms you'll use.
  7.  Generate terraform.tfvars and .env from your answers.
  8.  Create a GCS bucket for terraform remote state.
  9.  Collect the Slack signing secret value (from migration backup or via prompt),
      then run terraform plan + apply. The secret value is passed to terraform
      via TF_VAR, so the Cloud Run service comes up with the binding already
      satisfied.
  10. Restore Firestore data if migrating.
  11. Run scripts/deploy_forum.sh (Cloud Build → real image → Cloud Run).
  12. Verify the service responds on /health.
  13. Print platform-specific webhook URLs for manual setup.

Pre-requisites you must handle yourself:
  • A GCP project exists and has a billing account linked.
  • You have Owner or equivalent permissions on that project.
  • Google Cloud CLI (gcloud) installed.
      Install: https://cloud.google.com/sdk/docs/install
  • Terraform 1.2 or newer installed (lifecycle preconditions require 1.2+).
      Install: https://developer.hashicorp.com/terraform/install

You can cancel any time with Ctrl-C.

EOF
    if ! prompt_yn "Proceed?" y; then
        echo "Aborted."
        exit 0
    fi
    hr
}

# --- Phase 2: gcloud check + auth + project ---
phase_2_gcloud() {
    say "Phase 2: gcloud CLI and authentication"
    if ! command -v gcloud >/dev/null 2>&1; then
        err "gcloud CLI not found."
        echo "Install: https://cloud.google.com/sdk/docs/install"
        exit 1
    fi
    ok "gcloud: $(gcloud --version 2>/dev/null | head -1)"

    local active_account
    active_account=$(gcloud auth list --filter='status:ACTIVE' --format='value(account)' 2>/dev/null || true)
    if [[ -z "$active_account" ]]; then
        say "No active gcloud account. Logging in..."
        gcloud auth login
        active_account=$(gcloud auth list --filter='status:ACTIVE' --format='value(account)')
    fi
    ok "gcloud account: $active_account"

    if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
        say "Application Default Credentials not configured. Logging in..."
        gcloud auth application-default login
    fi
    ok "Application Default Credentials configured."

    local current_project
    current_project=$(gcloud config get-value project 2>/dev/null || true)
    if [[ -n "$current_project" && "$current_project" != "(unset)" ]]; then
        echo "Current default project: ${BOLD}$current_project${NC}"
        if prompt_yn "Use this project?" y; then
            PROJECT_ID="$current_project"
        fi
    fi

    if [[ -z "${PROJECT_ID:-}" ]]; then
        echo "Available projects:"
        gcloud projects list --format="table(projectId,name,projectNumber)"
        echo
        read -rp "Enter project ID to use: " PROJECT_ID
        gcloud config set project "$PROJECT_ID" >/dev/null
    fi
    ok "Project: $PROJECT_ID"
    hr
}

# --- Phase 3: Bootstrap APIs needed by terraform itself ---
phase_3_bootstrap_apis() {
    say "Phase 3: Bootstrap APIs"
    # Terraform needs serviceusage to enable the rest of the APIs, and
    # cloudresourcemanager to query/modify project IAM. Both can ONLY be
    # enabled outside terraform — chicken-and-egg otherwise.
    gcloud services enable \
        serviceusage.googleapis.com \
        cloudresourcemanager.googleapis.com \
        --project="$PROJECT_ID"
    ok "Bootstrap APIs enabled."
    hr
}

# --- Phase 4: terraform check ---
phase_4_terraform() {
    say "Phase 4: terraform CLI"
    if ! command -v terraform >/dev/null 2>&1; then
        err "terraform CLI not found."
        echo "Install: https://developer.hashicorp.com/terraform/install"
        exit 1
    fi
    ok "terraform: $(terraform -version 2>/dev/null | head -1)"
    hr
}

# --- Phase 5: Migration? ---
phase_5_migration() {
    say "Phase 5: Migration"
    IS_MIGRATION=false
    MIGRATION_PATH=""
    if prompt_yn "Is this a migration from an existing Forum install?"; then
        local default_path="$REPO_ROOT/migration-data"
        read -rp "Path to migration data folder [$default_path]: " MIGRATION_PATH
        MIGRATION_PATH="${MIGRATION_PATH:-$default_path}"
        if [[ ! -d "$MIGRATION_PATH" ]]; then
            err "Migration folder not found: $MIGRATION_PATH"
            exit 1
        fi
        IS_MIGRATION=true
        ok "Migration source: $MIGRATION_PATH"
        # Sanity check expected subfolders
        for sub in secrets firestore; do
            if [[ ! -d "$MIGRATION_PATH/$sub" ]]; then
                warn "  $MIGRATION_PATH/$sub not found — that step will be skipped."
            fi
        done
    else
        ok "New install (no migration)."
    fi
    hr
}

# --- Phase 6: Platforms ---
phase_6_platforms() {
    say "Phase 6: Platforms"
    echo "Which messaging platforms will you use? Space-separated."
    echo "Options: ${BOLD}slack${NC} ${BOLD}gchat${NC} (Google Chat) ${BOLD}telegram${NC} ${BOLD}discord${NC}"
    echo
    echo "${BOLD}Note on discord:${NC} Discord cannot deliver DMs to HTTP webhooks, so"
    echo "selecting it provisions a single multi-tenant e2-micro Compute Engine"
    echo "VM (the discord-worker). The worker auto-discovers Discord-enabled"
    echo "agents from Firestore at runtime, so one VM serves all your Discord"
    echo "bots. That VM is FREE in the GCP Always Free tier in us-central1,"
    echo "us-west1, or us-east1 (one per billing account), but costs ~\$6-7/month"
    echo "if your free-tier slot is already in use or you deploy outside those"
    echo "regions. See docs/DISCORD_WORKER.md for cost and patching detail. Per-"
    echo "agent bot tokens live in each agent's OWN project (see the agent-"
    echo "project terraform template) — install.sh does NOT prompt for them."
    echo
    read -rp "Platforms: " platforms_input
    USE_SLACK=false
    USE_GCHAT=false
    USE_TELEGRAM=false
    USE_DISCORD=false
    for p in $platforms_input; do
        case "$p" in
            slack)    USE_SLACK=true ;;
            gchat)    USE_GCHAT=true ;;
            telegram) USE_TELEGRAM=true ;;
            discord)  USE_DISCORD=true ;;
            *) warn "Unknown platform: $p (ignored)" ;;
        esac
    done
    if [[ "$USE_SLACK" == "false" && "$USE_GCHAT" == "false" && "$USE_TELEGRAM" == "false" && "$USE_DISCORD" == "false" ]]; then
        warn "No platforms selected. The Forum will install but you'll need to wire one up later."
    fi

    # Discord needs a worker VM image to be set in terraform at apply time.
    # The image URL is deterministic; we compute it here.
    DISCORD_WORKER_ZONE=""
    DISCORD_WORKER_IMAGE=""
    if [[ "$USE_DISCORD" == "true" ]]; then
        echo
        say "Discord worker VM configuration"
        echo "Pick a zone for the discord-worker VM. Stay in us-central1,"
        echo "us-west1, or us-east1 to keep the e2-micro in the Always Free tier."
        echo "Default: us-central1-a"
        read -rp "  Worker zone [us-central1-a]: " DISCORD_WORKER_ZONE
        DISCORD_WORKER_ZONE="${DISCORD_WORKER_ZONE:-us-central1-a}"
        # The image URL is deterministic. Terraform creates the Artifact
        # Registry repo, then phase_13 prints the gcloud builds submit
        # command to populate it. The VM pulls on next reboot.
        DISCORD_WORKER_IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/discord-worker/worker:latest"
        ok "  Worker image will be: $DISCORD_WORKER_IMAGE"
    fi

    ok "Selected: ${platforms_input:-(none)}"
    hr
}

# --- Phase 7: Generate terraform.tfvars and .env ---
phase_7_config_files() {
    say "Phase 7: Generate terraform.tfvars and .env"

    read -rp "GCP region [us-central1]: " REGION
    REGION="${REGION:-us-central1}"

    local tfvars="$REPO_ROOT/terraform/terraform.tfvars"
    if [[ -f "$tfvars" ]] && grep -q '^project_id' "$tfvars" 2>/dev/null; then
        warn "$tfvars already exists."
        if ! prompt_yn "Overwrite?"; then
            ok "Keeping existing terraform.tfvars."
        else
            write_tfvars "$tfvars"
        fi
    else
        write_tfvars "$tfvars"
    fi

    local envfile="$REPO_ROOT/.env"
    if [[ -f "$envfile" ]]; then
        warn "$envfile already exists."
        if ! prompt_yn "Overwrite?"; then
            ok "Keeping existing .env."
        else
            write_env "$envfile"
        fi
    else
        write_env "$envfile"
    fi
    hr
}

write_tfvars() {
    local path="$1"
    cat > "$path" <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
project_id  = "$PROJECT_ID"
region      = "$REGION"
environment = "production"
use_slack   = $USE_SLACK
use_discord = $USE_DISCORD
EOF
    if [[ "$USE_DISCORD" == "true" ]]; then
        cat >> "$path" <<EOF

# Discord-specific vars. The worker is multi-tenant: it discovers
# Discord-enabled agents from Firestore at runtime. Per-agent bot tokens
# live in each agent's OWN project (see the agent-project terraform
# template). The worker image will be empty in the registry until you
# run \`gcloud builds submit discord-worker --tag=\$discord_worker_image\`
# (see phase 13 of install.sh's manual-steps output, or
# docs/DISCORD_WORKER.md).
discord_worker_image        = "$DISCORD_WORKER_IMAGE"
discord_worker_zone         = "$DISCORD_WORKER_ZONE"
EOF
    fi
    ok "Wrote $path"
}

write_env() {
    local path="$1"
    cat > "$path" <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)

# Application Settings
ENVIRONMENT=production
LOG_LEVEL=INFO

# Google Cloud Platform
GCP_PROJECT_ID=$PROJECT_ID
GCP_LOCATION=$REGION

# Firestore Collections
FIRESTORE_AGENTS_COLLECTION=agents
FIRESTORE_SESSIONS_COLLECTION=sessions

# Slack Configuration
# In production, this is read from Secret Manager (slack-signing-secret).
# Leave the placeholder below for local dev — it's only honored when running
# outside Cloud Run.
SLACK_SIGNING_SECRET=placeholder-use-secret-manager-in-cloud-run

# API Configuration
API_V1_PREFIX=/api/v1

# GCS File Upload
GCS_BUCKET_NAME=${PROJECT_ID}-slack-files
GCS_FILE_PREFIX=slack-files

# Session Management
SESSION_TIMEOUT_MINUTES=180
EOF
    ok "Wrote $path"
}

# --- Phase 8: Bootstrap GCS terraform state backend ---
phase_8_state_backend() {
    say "Phase 8: Set up GCS state backend"
    local state_bucket="${PROJECT_ID}-terraform-state"

    if gcloud storage buckets describe "gs://$state_bucket" --project="$PROJECT_ID" >/dev/null 2>&1; then
        ok "State bucket already exists: gs://$state_bucket"
    else
        say "Creating state bucket gs://$state_bucket..."
        gcloud storage buckets create "gs://$state_bucket" \
            --project="$PROJECT_ID" \
            --location="$REGION" \
            --uniform-bucket-level-access \
            --public-access-prevention
        gcloud storage buckets update "gs://$state_bucket" --versioning >/dev/null
        ok "State bucket created with versioning enabled."
    fi

    local providers="$REPO_ROOT/terraform/providers.tf"
    if grep -qE '^[[:space:]]*backend "gcs"' "$providers"; then
        ok "GCS backend already configured in providers.tf."
    else
        say "Configuring GCS backend in providers.tf..."
        cat > "$providers" <<EOF
# Terraform Provider Configuration

terraform {
  required_version = ">= 1.2"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
  }

  backend "gcs" {
    bucket = "$state_bucket"
    prefix = "the-forum/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
EOF
        ok "providers.tf updated."
    fi

    say "Initializing terraform..."
    (cd "$REPO_ROOT/terraform" && terraform init -upgrade)
    ok "Terraform initialized."
    hr
}

# True iff `gcloud secrets describe` finds the secret in the project. We
# need this to gate the `-target=` apply on phase 9a — pulling a secret
# that doesn't yet exist into a targeted plan errors out.
secret_exists() {
    local name="$1"
    gcloud secrets describe "$name" --project="$PROJECT_ID" >/dev/null 2>&1
}

# True iff the secret has at least one ENABLED version. We treat the
# secret as "populated" when this returns true, so re-runs of install.sh
# don't overwrite values the operator (or a prior install) already set.
secret_has_version() {
    local name="$1"
    local count
    count=$(gcloud secrets versions list "$name" \
        --filter="state:ENABLED" --limit=1 --format="value(name)" \
        --project="$PROJECT_ID" 2>/dev/null | wc -l | tr -d ' ')
    [[ "$count" -ge 1 ]]
}

# Add a new version to a Secret Manager secret. Value is supplied on stdin
# so it never lands in the process listing.
add_secret_version() {
    local name="$1"
    local value="$2"
    printf '%s' "$value" | gcloud secrets versions add "$name" \
        --data-file=- --project="$PROJECT_ID" >/dev/null
}

# Populate slack-signing-secret from operator input (or migration backup).
# Idempotent: if the secret already has an enabled version, returns silently.
# Cloud Run requires the secret to have a version at revision-create time;
# phase 9 calls this BEFORE the full terraform apply for that reason.
populate_slack_secret() {
    local name="slack-signing-secret"
    if secret_has_version "$name"; then
        ok "  $name already has an enabled version — leaving it alone."
        return 0
    fi

    local backup="$MIGRATION_PATH/secrets/slack-signing-secret.value"
    local secret_value=""

    if [[ "$IS_MIGRATION" == "true" && -s "$backup" ]]; then
        if prompt_yn "  Use slack-signing-secret value from migration backup ($backup)?" y; then
            secret_value=$(cat "$backup")
            ok "  Using value from migration backup."
        fi
    fi

    if [[ -z "$secret_value" ]]; then
        cat <<EOF

  ${BOLD}How to retrieve the Slack signing secret:${NC}
    1. Open https://api.slack.com/apps in a browser
    2. Click your Slack app
    3. Go to 'Basic Information' in the left sidebar
    4. Scroll to 'App Credentials'
    5. Click 'Show' next to 'Signing Secret' and copy the value

  If multiple Slack apps route to The Forum, separate their secrets with
  commas (e.g., 'secret1,secret2').

EOF
        read -rsp "  Slack signing secret(s): " secret_value
        echo
    fi

    if [[ -z "$secret_value" ]]; then
        err "  Empty secret. Slack support requires a non-empty value."
        echo "  Either set use_slack=false in terraform.tfvars, or re-run with a real value."
        exit 1
    fi

    add_secret_version "$name" "$secret_value"
    ok "  $name populated."
}

# Populate the three admin UI secrets. Idempotent — only prompts for ones
# that don't yet have an enabled version. admin-session-secret is generated
# automatically via openssl, not prompted.
populate_admin_secrets() {
    if ! secret_has_version "oauth-client-id"; then
        echo
        echo "  ${BOLD}OAuth Client ID${NC} (GCP Console → APIs & Services → Credentials)"
        local client_id=""
        read -rp "    oauth-client-id: " client_id
        if [[ -z "$client_id" ]]; then
            err "  Empty OAuth client ID. Admin UI requires a non-empty value."
            exit 1
        fi
        add_secret_version "oauth-client-id" "$client_id"
        ok "  oauth-client-id populated."
    else
        ok "  oauth-client-id already has an enabled version — leaving it alone."
    fi

    if ! secret_has_version "oauth-client-secret"; then
        echo "  ${BOLD}OAuth Client Secret${NC}"
        local client_secret=""
        read -rsp "    oauth-client-secret: " client_secret
        echo
        if [[ -z "$client_secret" ]]; then
            err "  Empty OAuth client secret. Admin UI requires a non-empty value."
            exit 1
        fi
        add_secret_version "oauth-client-secret" "$client_secret"
        ok "  oauth-client-secret populated."
    else
        ok "  oauth-client-secret already has an enabled version — leaving it alone."
    fi

    if ! secret_has_version "admin-session-secret"; then
        # No prompt — we just generate one.
        local session
        session=$(openssl rand -hex 32)
        add_secret_version "admin-session-secret" "$session"
        ok "  admin-session-secret generated and populated."
    else
        ok "  admin-session-secret already has an enabled version — leaving it alone."
    fi
}

# --- Phase 9: terraform apply ---
#
# Three-step flow:
#   9a. Targeted apply of just the Secret Manager secret CONTAINERS. We do
#       this first because Cloud Run validates that each bound secret has
#       at least one accessible version when it creates a revision —
#       creating Cloud Run before the secrets are populated would fail.
#   9b. Populate the secret values via `gcloud secrets versions add`. This
#       is idempotent: re-runs leave already-populated secrets untouched.
#   9c. Full terraform apply for everything else (Cloud Run, IAM, the
#       discord-worker VM, etc.).
phase_9_apply() {
    say "Phase 9: terraform plan + apply"

    # Build the list of secret containers we need to materialize ahead of
    # everything else. Discord is intentionally absent here — Discord bot
    # tokens live per-agent in each agent's OWN project, not the Forum's.
    local secret_targets=()
    if [[ "$USE_SLACK" == "true" ]]; then
        secret_targets+=("-target=google_secret_manager_secret.slack_signing_secret")
    fi
    # The admin UI gate isn't carried in install.sh's USE_* vars — it's set
    # directly in terraform.tfvars. Read it back from disk.
    local admin_ui_enabled="false"
    if grep -qE '^[[:space:]]*enable_admin_ui[[:space:]]*=[[:space:]]*true' \
            "$REPO_ROOT/terraform/terraform.tfvars" 2>/dev/null; then
        admin_ui_enabled="true"
        secret_targets+=(
            "-target=google_secret_manager_secret.oauth_client_id"
            "-target=google_secret_manager_secret.oauth_client_secret"
            "-target=google_secret_manager_secret.admin_session_secret"
        )
    fi

    if [[ ${#secret_targets[@]} -gt 0 ]]; then
        say "Phase 9a: create secret containers (terraform apply -target)"
        (cd "$REPO_ROOT/terraform" && terraform apply -auto-approve "${secret_targets[@]}")
        ok "Secret containers created."
        echo

        say "Phase 9b: populate secret values via gcloud (skips any already populated)"
        [[ "$USE_SLACK" == "true" ]] && populate_slack_secret
        [[ "$admin_ui_enabled" == "true" ]] && populate_admin_secrets
        echo
    else
        say "No Forum-managed secrets to populate — skipping secret-container phase."
    fi

    say "Phase 9c: full terraform plan + apply"
    (cd "$REPO_ROOT/terraform" && terraform plan)
    echo
    if ! prompt_yn "Apply this plan?" y; then
        echo "Aborted before apply."
        exit 0
    fi
    (cd "$REPO_ROOT/terraform" && terraform apply -auto-approve)
    ok "Terraform apply complete."
    hr
}

# --- Phase 10: Firestore data restore (migration only) ---
phase_10_firestore() {
    say "Phase 10: Firestore data restore"
    if [[ "$IS_MIGRATION" != "true" ]]; then
        ok "Not a migration — skipping Firestore restore."
        hr
        return 0
    fi

    local firestore_dir
    firestore_dir=$(find "$MIGRATION_PATH/firestore" -mindepth 1 -maxdepth 1 -type d -name 'firestore-backup-*' 2>/dev/null | head -1)
    if [[ -z "$firestore_dir" ]]; then
        warn "No firestore-backup-* folder found in $MIGRATION_PATH/firestore. Skipping restore."
        hr
        return 0
    fi

    local backup_name
    backup_name=$(basename "$firestore_dir")
    local staging_bucket="${PROJECT_ID}-staging"

    say "Uploading $backup_name to gs://$staging_bucket/..."
    gcloud storage cp --recursive "$firestore_dir" "gs://$staging_bucket/" --project="$PROJECT_ID"

    say "Importing Firestore data (this can take a few minutes)..."
    gcloud firestore import "gs://$staging_bucket/$backup_name" --project="$PROJECT_ID"
    ok "Firestore data restored."
    hr
}

# --- Phase 11: Build and deploy real image ---
phase_11_deploy() {
    say "Phase 11: Build and deploy The Forum image"
    "$REPO_ROOT/scripts/deploy_forum.sh" --project "$PROJECT_ID" --region "$REGION"
    ok "Deploy complete."
    hr
}

# --- Phase 12: Health check ---
phase_12_verify() {
    say "Phase 12: Verify service health"
    local url
    url=$(cd "$REPO_ROOT/terraform" && terraform output -raw cloud_run_url 2>/dev/null || true)
    if [[ -z "$url" ]]; then
        warn "Could not read cloud_run_url from terraform output. Skipping health check."
        hr
        return 0
    fi
    if curl --silent --fail --max-time 10 "$url/health" >/dev/null; then
        ok "Health check passed: $url/health"
    else
        err "Health check failed at $url/health"
        echo "  Inspect logs:"
        echo "    gcloud run services logs read the-forum --region=$REGION --project=$PROJECT_ID"
    fi
    hr
}

# --- Phase 13: Manual platform setup instructions ---
phase_13_platforms() {
    say "Phase 13: Manual platform setup"
    local cr_url
    cr_url=$(cd "$REPO_ROOT/terraform" && terraform output -raw cloud_run_url 2>/dev/null || echo "<cloud-run-url>")

    echo
    echo "${BOLD}==================== MANUAL SETUP STEPS ====================${NC}"
    echo

    if [[ "$USE_SLACK" == "true" ]]; then
        cat <<EOF
${BOLD}[ ] SLACK${NC}
    1. Open: https://api.slack.com/apps
    2. For each Slack app → Event Subscriptions → Request URL:
         ${cr_url}/api/v1/slack/events
    3. Save and verify the URL.

EOF
    fi

    if [[ "$USE_GCHAT" == "true" ]]; then
        cat <<EOF
${BOLD}[ ] GOOGLE CHAT${NC}
    1. GCP Console → APIs & Services → Google Chat API → Configuration
    2. Set 'App URL' to:
         ${cr_url}/api/v1/google-chat/events
    3. Save.

EOF
    fi

    if [[ "$USE_TELEGRAM" == "true" ]]; then
        cat <<EOF
${BOLD}[ ] TELEGRAM (per agent)${NC}
    For each Telegram bot, set the webhook URL:
       curl -X POST "https://api.telegram.org/bot\${BOT_TOKEN}/setWebhook" \\
         -d "url=${cr_url}/api/v1/telegram/events/\${AGENT_ID}" \\
         -d "secret_token=\${WEBHOOK_SECRET}"

EOF
    fi

    if [[ "$USE_DISCORD" == "true" ]]; then
        local worker_sa
        worker_sa=$(cd "$REPO_ROOT/terraform" && terraform output -raw discord_worker_service_account 2>/dev/null || echo "<discord-worker-sa>")
        cat <<EOF
${BOLD}[ ] DISCORD${NC}
    Terraform has provisioned the multi-tenant worker: an Artifact
    Registry repo (discord-worker), the worker service account, the
    e2-micro VM, and the Firestore/Logging/Monitoring IAM bindings. The
    VM is running but its container is failing to start because the
    worker image has not been built yet. Finish the worker bring-up,
    then add Discord agents one at a time using the per-agent steps.

    Worker bring-up (one-time):

    1. Build and push the worker container image:
         gcloud builds submit "$REPO_ROOT/discord-worker" \\
           --tag="$DISCORD_WORKER_IMAGE" \\
           --project=$PROJECT_ID

    2. Reboot the VM so it pulls the freshly-built image:
         gcloud compute instances reset discord-worker \\
           --zone=$DISCORD_WORKER_ZONE \\
           --project=$PROJECT_ID

    3. Confirm the worker logs the startup banner:
         gcloud compute instances get-serial-port-output discord-worker \\
           --zone=$DISCORD_WORKER_ZONE \\
           --project=$PROJECT_ID | tail -50
       Look for: "discord-worker starting: forum=..."

    To onboard a Discord agent (per agent, in the AGENT'S project):

    A. Provision the bot token secret in the agent's project. Use the
       docs/terraform-templates/agent-project main.tf SECTION 5: DISCORD
       block, then populate the token:
         echo -n "YOUR_BOT_TOKEN" | gcloud secrets versions add \\
           \${BOT_ACCOUNT_ID}-discord-token \\
           --data-file=- --project=<agent-project>

    B. Add a discord platform block to the agent's Firestore document.
       The discord_worker_service_account field must be EXACTLY this:
         "discord_worker_service_account": "$worker_sa"

    C. Wait up to AGENT_REFRESH_INTERVAL_SECONDS (default 300s) for the
       worker to pick up the new bot. Or force an immediate reconcile by
       resetting the VM (gcloud compute instances reset discord-worker).

    D. Invite the bot to a server (Discord Developer Portal → OAuth2 →
       URL Generator, scopes: bot, permissions: Send Messages + Read
       Message History) and DM it.

    Watch the worker forward DMs to confirm:
         gcloud logging read \\
           'resource.type="gce_instance" AND jsonPayload.message:"Forwarded DM"' \\
           --limit=20 --project=$PROJECT_ID

    Full runbook (cost, patching cadence, redeploy steps):
       docs/DISCORD_WORKER.md

EOF
    fi

    if [[ "$USE_SLACK" == "false" && "$USE_GCHAT" == "false" && "$USE_TELEGRAM" == "false" && "$USE_DISCORD" == "false" ]]; then
        echo "  No platforms selected. When you're ready, see docs/FOR_AGENT_DEVELOPERS.md."
        echo
    fi

    cat <<EOF
${BOLD}[ ] LOCAL PYTHON ENV (optional, for running agent-registration scripts)${NC}
    To run scripts/deploy_agent.py, scripts/provision_scheduler_api_key.py,
    or anything else under scripts/ that's Python, set up a venv:
       python3 -m venv venv
       source venv/bin/activate
       pip install -r requirements.txt

EOF

    echo "${BOLD}============================================================${NC}"
}

# --- Main ---
main() {
    phase_1_announce
    phase_2_gcloud
    phase_3_bootstrap_apis
    phase_4_terraform
    phase_5_migration
    phase_6_platforms
    phase_7_config_files
    phase_8_state_backend
    phase_9_apply
    phase_10_firestore
    phase_11_deploy
    phase_12_verify
    phase_13_platforms

    echo
    ok "${BOLD}Install complete.${NC}"
}

main "$@"
