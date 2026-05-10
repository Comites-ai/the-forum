#!/usr/bin/env bash
#
# Deploy The Forum to Cloud Run via Cloud Build.
#
# Usage:
#   ./scripts/deploy_forum.sh                  # deploy from current commit
#   ./scripts/deploy_forum.sh --project my-id  # override GCP project
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Defaults (override with flags or environment) ---
PROJECT_ID="${GCP_PROJECT_ID:-}"
REGION="${GCP_LOCATION:-us-central1}"
GCS_BUCKET="${GCS_BUCKET_NAME:-}"
CONFIG="cloudbuild.yaml"

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)  PROJECT_ID="$2"; shift 2 ;;
        --region)   REGION="$2";     shift 2 ;;
        --config)   CONFIG="$2";     shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--project PROJECT_ID] [--region REGION] [--config cloudbuild.yaml]"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# --- Validate prerequisites ---
if [[ -z "$PROJECT_ID" ]]; then
    # Try .env file as fallback
    if [[ -f "$REPO_ROOT/.env" ]]; then
        PROJECT_ID=$(grep -E '^GCP_PROJECT_ID=' "$REPO_ROOT/.env" | cut -d= -f2 | tr -d ' "'"'"'')
    fi
    if [[ -z "$PROJECT_ID" ]]; then
        echo "Error: GCP project ID not set. Use --project, GCP_PROJECT_ID env var, or .env file."
        exit 1
    fi
fi

# Read GCS bucket name from .env if not set
if [[ -z "$GCS_BUCKET" ]] && [[ -f "$REPO_ROOT/.env" ]]; then
    GCS_BUCKET=$(grep -E '^GCS_BUCKET_NAME=' "$REPO_ROOT/.env" | cut -d= -f2 | tr -d ' "'"'"'')
fi

if ! command -v gcloud &>/dev/null; then
    echo "Error: gcloud CLI not found. Install it from https://cloud.google.com/sdk/docs/install"
    exit 1
fi

if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    echo "Error: Not inside a git repository."
    exit 1
fi

# --- Check for uncommitted changes ---
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Warning: You have uncommitted changes. The deployed image will not match your working tree."
    read -rp "Continue anyway? [y/N] " answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        echo "Aborted. Commit your changes first, then re-run."
        exit 1
    fi
fi

# --- Gather build info ---
COMMIT_SHA=$(git rev-parse HEAD)
COMMIT_SHORT=$(git rev-parse --short HEAD)
COMMIT_MSG=$(git log -1 --pretty=%s)

echo "=== The Forum Deployment ==="
echo "  Project:    $PROJECT_ID"
echo "  Region:     $REGION"
echo "  GCS Bucket: ${GCS_BUCKET:-<not configured>}"
echo "  Commit:     $COMMIT_SHORT ($COMMIT_MSG)"
echo "  Config:     $CONFIG"
echo ""

# --- Detect whether Slack is in use ---
# cloudbuild.yaml only adds --set-secrets for Slack when _EXTRA_FLAGS is set.
# We auto-detect by checking whether terraform created the slack-signing-secret.
EXTRA_FLAGS=""
if gcloud secrets describe slack-signing-secret --project="$PROJECT_ID" &>/dev/null; then
    EXTRA_FLAGS="--set-secrets=SLACK_SIGNING_SECRET=slack-signing-secret:latest"
    echo "  Slack:    detected (slack-signing-secret present)"
else
    echo "  Slack:    not in use (slack-signing-secret absent)"
fi
echo ""

# --- Submit build ---
echo "Submitting Cloud Build..."
gcloud builds submit "$REPO_ROOT" \
    --config="$REPO_ROOT/$CONFIG" \
    --project="$PROJECT_ID" \
    --substitutions="COMMIT_SHA=$COMMIT_SHA,_GCP_LOCATION=$REGION,_GCS_BUCKET_NAME=$GCS_BUCKET,_EXTRA_FLAGS=$EXTRA_FLAGS"

echo ""

# --- Route traffic to latest revision ---
SERVICE_NAME="the-forum"
echo "Routing 100% traffic to latest revision..."
gcloud run services update-traffic "$SERVICE_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --to-latest

# --- Clean up old revisions (keep only the latest) ---
echo ""
echo "Cleaning up old revisions..."
LATEST_REVISION=$(gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(status.traffic[0].revisionName)")

OLD_REVISIONS=$(gcloud run revisions list \
    --service="$SERVICE_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(metadata.name)" \
    | grep -v "^${LATEST_REVISION}$" || true)

if [[ -n "$OLD_REVISIONS" ]]; then
    while IFS= read -r rev; do
        echo "  Deleting revision: $rev"
        gcloud run revisions delete "$rev" \
            --project="$PROJECT_ID" \
            --region="$REGION" \
            --quiet 2>/dev/null || echo "  (could not delete $rev — may still be draining traffic)"
    done <<< "$OLD_REVISIONS"
    echo "Cleanup complete."
else
    echo "  No old revisions to clean up."
fi

echo ""
echo "=== Deployment complete ==="
echo "Service URL:"
gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(status.url)" 2>/dev/null || echo "  (could not retrieve — check Cloud Console)"
