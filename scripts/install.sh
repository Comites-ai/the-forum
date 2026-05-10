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
    echo "Options: ${BOLD}slack${NC} ${BOLD}gchat${NC} (Google Chat) ${BOLD}telegram${NC}"
    read -rp "Platforms: " platforms_input
    USE_SLACK=false
    USE_GCHAT=false
    USE_TELEGRAM=false
    for p in $platforms_input; do
        case "$p" in
            slack)    USE_SLACK=true ;;
            gchat)    USE_GCHAT=true ;;
            telegram) USE_TELEGRAM=true ;;
            *) warn "Unknown platform: $p (ignored)" ;;
        esac
    done
    if [[ "$USE_SLACK" == "false" && "$USE_GCHAT" == "false" && "$USE_TELEGRAM" == "false" ]]; then
        warn "No platforms selected. The Forum will install but you'll need to wire one up later."
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

# Collect the Slack signing secret value before terraform apply.
# Sets TF_VAR_slack_signing_secret_value for terraform to consume.
# Cloud Run validates the bound secret has a version at creation time, so
# the value must exist BEFORE terraform creates the Cloud Run service —
# we pass it through terraform itself rather than populating after apply.
collect_slack_secret_value() {
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

    export TF_VAR_slack_signing_secret_value="$secret_value"
    ok "  Slack signing secret value collected (will be passed to terraform via TF_VAR)."
}

# --- Phase 9: terraform apply ---
phase_9_apply() {
    say "Phase 9: terraform plan + apply"

    if [[ "$USE_SLACK" == "true" ]]; then
        say "Phase 9 needs the Slack signing secret value before terraform plan/apply."
        collect_slack_secret_value
        echo
    fi

    (cd "$REPO_ROOT/terraform" && terraform plan)
    echo
    if ! prompt_yn "Apply this plan?" y; then
        echo "Aborted before apply."
        unset TF_VAR_slack_signing_secret_value
        exit 0
    fi
    (cd "$REPO_ROOT/terraform" && terraform apply -auto-approve)
    unset TF_VAR_slack_signing_secret_value
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

    if [[ "$USE_SLACK" == "false" && "$USE_GCHAT" == "false" && "$USE_TELEGRAM" == "false" ]]; then
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
