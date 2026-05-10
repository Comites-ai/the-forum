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

  1.  Check for required CLIs (gcloud, terraform). If missing, link to docs.
  2.  Authenticate gcloud (user + Application Default Credentials).
  3.  Bootstrap-enable the APIs terraform itself needs.
  4.  Ask whether this is a migration from an existing install.
  5.  Ask which messaging platforms you'll use.
  6.  Generate terraform.tfvars and .env from your answers.
  7.  Create a GCS bucket for terraform remote state.
  8.  Run terraform plan + apply (creates infra with a hello-world placeholder).
  9.  Populate the Slack signing secret (from migration backup or prompt).
  10. Restore Firestore data if migrating.
  11. Run scripts/deploy_forum.sh (Cloud Build → real image → Cloud Run).
  12. Verify the service responds on /health.
  13. Print platform-specific webhook URLs for manual setup.

Pre-requisites you must handle yourself:
  • A GCP project exists and has a billing account linked.
  • You have Owner or equivalent permissions on that project.

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

# --- Phase 2.5: Bootstrap APIs needed by terraform itself ---
phase_2_5_bootstrap_apis() {
    say "Phase 2.5: Bootstrap APIs"
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

# --- Phase 3: terraform check ---
phase_3_terraform() {
    say "Phase 3: terraform CLI"
    if ! command -v terraform >/dev/null 2>&1; then
        err "terraform CLI not found."
        echo "Install: https://developer.hashicorp.com/terraform/install"
        exit 1
    fi
    ok "terraform: $(terraform -version 2>/dev/null | head -1)"
    hr
}

# --- Phase 4: Migration? ---
phase_4_migration() {
    say "Phase 4: Migration"
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

# --- Phase 5: Platforms ---
phase_5_platforms() {
    say "Phase 5: Platforms"
    echo "Which messaging platforms will you use? Space-separated."
    echo "Options: ${BOLD}slack${NC} ${BOLD}chat${NC} ${BOLD}telegram${NC}"
    read -rp "Platforms: " platforms_input
    USE_SLACK=false
    USE_CHAT=false
    USE_TELEGRAM=false
    for p in $platforms_input; do
        case "$p" in
            slack)    USE_SLACK=true ;;
            chat)     USE_CHAT=true ;;
            telegram) USE_TELEGRAM=true ;;
            *) warn "Unknown platform: $p (ignored)" ;;
        esac
    done
    if [[ "$USE_SLACK" == "false" && "$USE_CHAT" == "false" && "$USE_TELEGRAM" == "false" ]]; then
        warn "No platforms selected. The Forum will install but you'll need to wire one up later."
    fi
    ok "Selected: ${platforms_input:-(none)}"
    hr
}

# --- Phase 5.5: Generate terraform.tfvars and .env ---
phase_5_5_config_files() {
    say "Phase 5.5: Generate terraform.tfvars and .env"

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
EOF
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

# --- Phase 5.6: Bootstrap GCS terraform state backend ---
phase_5_6_state_backend() {
    say "Phase 5.6: Set up GCS state backend"
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
  required_version = ">= 1.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
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

# --- Phase 6: terraform apply ---
phase_6_apply() {
    say "Phase 6: terraform plan + apply"
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

# --- Phase 6.5: Populate Slack signing secret ---
phase_6_5_secret() {
    say "Phase 6.5: Populate slack-signing-secret"
    if [[ "$USE_SLACK" != "true" ]]; then
        ok "Slack not selected — terraform did not create the secret. Skipping."
        hr
        return 0
    fi

    local backup="$MIGRATION_PATH/secrets/slack-signing-secret.value"
    if [[ "$IS_MIGRATION" == "true" && -f "$backup" ]]; then
        say "Restoring Slack signing secret from migration backup..."
        gcloud secrets versions add slack-signing-secret \
            --data-file="$backup" \
            --project="$PROJECT_ID" >/dev/null
        ok "Slack signing secret restored from backup."
    else
        echo "Enter Slack signing secret(s)."
        echo "If you have multiple Slack apps, separate with commas."
        echo "Find it at: api.slack.com/apps → Your App → Basic Information → Signing Secret"
        read -rsp "Secret: " slack_secret
        echo
        if [[ -z "$slack_secret" ]]; then
            err "Empty secret. Re-run install.sh and provide a value, or run:"
            echo "  echo -n VALUE | gcloud secrets versions add slack-signing-secret --data-file=- --project=$PROJECT_ID"
            exit 1
        fi
        printf "%s" "$slack_secret" | gcloud secrets versions add slack-signing-secret \
            --data-file=- --project="$PROJECT_ID" >/dev/null
        ok "Slack signing secret populated."
    fi
    hr
}

# --- Phase 6.6: Firestore data restore (migration only) ---
phase_6_6_firestore() {
    say "Phase 6.6: Firestore data restore"
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

# --- Phase 7: Build and deploy real image ---
phase_7_deploy() {
    say "Phase 7: Build and deploy The Forum image"
    "$REPO_ROOT/scripts/deploy_forum.sh" --project "$PROJECT_ID" --region "$REGION"
    ok "Deploy complete."
    hr
}

# --- Phase 7.5: Health check ---
phase_7_5_verify() {
    say "Phase 7.5: Verify service health"
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

# --- Phase 8: Manual platform setup instructions ---
phase_8_platforms() {
    say "Phase 8: Manual platform setup"
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

    if [[ "$USE_CHAT" == "true" ]]; then
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

    if [[ "$USE_SLACK" == "false" && "$USE_CHAT" == "false" && "$USE_TELEGRAM" == "false" ]]; then
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
    phase_2_5_bootstrap_apis
    phase_3_terraform
    phase_4_migration
    phase_5_platforms
    phase_5_5_config_files
    phase_5_6_state_backend
    phase_6_apply
    phase_6_5_secret
    phase_6_6_firestore
    phase_7_deploy
    phase_7_5_verify
    phase_8_platforms

    echo
    ok "${BOLD}Install complete.${NC}"
}

main "$@"
