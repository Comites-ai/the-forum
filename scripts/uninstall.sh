#!/usr/bin/env bash
#
# uninstall.sh — Guided teardown of The Forum from a GCP project.
#
# Tears down infrastructure that scripts/install.sh created. Uses
# 'terraform destroy' against the GCS state backend as the source of
# truth, so it stays in sync with whatever variant of install.sh's
# terraform was applied. Refuses to run if state is unreachable.
#
# Phases:
#   1. Announce + first confirmation
#   2. Read project info from terraform.tfvars; verify state backend
#   3. Optional backup to ./migration-data/ (default Y)
#   4. Empty the staging bucket (force_destroy=false in terraform)
#   5. Disable Firestore delete protection
#   6. terraform destroy
#   7. Optional gcr.io image cleanup (asked each run)
#   8. Optional state bucket cleanup (default N — kept for re-install)
#   9. Summary
#
# Local artifacts (.env, terraform.tfvars, terraform/.terraform/) are
# left in place so a future install.sh re-run is fast. The summary at
# the end prints how to remove them manually.
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
${BOLD}=== The Forum — Guided Uninstall ===${NC}

${RED}This script tears down The Forum infrastructure from your GCP project.${NC}

It will:
  1.  Read project info from terraform/terraform.tfvars
  2.  Verify the terraform state backend is reachable (refuses to run if not)
  3.  Optionally back up secrets, Firestore data, and reference YAMLs to ./migration-data/
  4.  Empty the staging bucket (terraform can't auto-empty it)
  5.  Disable Firestore delete protection
  6.  Run 'terraform destroy'
  7.  Ask whether to delete container images in gcr.io
  8.  Ask whether to delete the terraform state bucket itself

What stays untouched:
  • Local files (.env, terraform.tfvars, terraform/.terraform/) — kept so re-install is easy.
  • Default compute SA, run-sources/_cloudbuild buckets, enabled APIs — out of scope or auto-managed.

You will be prompted multiple times before anything destructive runs.

EOF
    if ! prompt_yn "Proceed?" n; then
        echo "Aborted."
        exit 0
    fi
    hr
}

# --- Phase 2: Detect project + verify state ---
phase_2_detect() {
    say "Phase 2: Read project info and verify state backend"

    local tfvars="$REPO_ROOT/terraform/terraform.tfvars"
    if [[ ! -f "$tfvars" ]]; then
        err "$tfvars not found."
        echo "  uninstall.sh requires terraform.tfvars to know which project to tear down."
        echo "  If you installed manually, recreate it (project_id, region) and re-run."
        exit 1
    fi

    PROJECT_ID=$(grep -E '^[[:space:]]*project_id' "$tfvars" | sed -E 's/.*=\s*"([^"]+)".*/\1/')
    REGION=$(grep -E '^[[:space:]]*region' "$tfvars" | sed -E 's/.*=\s*"([^"]+)".*/\1/')
    REGION="${REGION:-us-central1}"

    if [[ -z "$PROJECT_ID" ]]; then
        err "Could not parse project_id from $tfvars"
        exit 1
    fi
    ok "Project: $PROJECT_ID (region $REGION)"

    if ! command -v terraform >/dev/null 2>&1; then
        err "terraform CLI not found. Install: https://developer.hashicorp.com/terraform/install"
        exit 1
    fi
    if ! command -v gcloud >/dev/null 2>&1; then
        err "gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install"
        exit 1
    fi

    say "Initializing terraform (verifying state backend)..."
    if ! (cd "$REPO_ROOT/terraform" && terraform init -input=false >/tmp/uninstall-tf-init.log 2>&1); then
        err "terraform init failed. State backend is unreachable or providers.tf is misconfigured."
        echo "  Log: /tmp/uninstall-tf-init.log"
        echo "  Fix: ensure providers.tf points at a valid GCS state bucket and your gcloud account has access."
        exit 1
    fi

    local resource_count
    resource_count=$(cd "$REPO_ROOT/terraform" && terraform state list 2>/dev/null | wc -l)
    if [[ "$resource_count" -eq 0 ]]; then
        warn "Terraform state is empty — there may be nothing to destroy."
        if ! prompt_yn "Continue anyway (e.g., to clean up the state bucket)?" n; then
            exit 0
        fi
    else
        ok "Terraform state has $resource_count resources."
    fi
    hr
}

# --- Phase 3: Optional backup ---
phase_3_backup() {
    say "Phase 3: Backup before destruction"
    if ! prompt_yn "Back up secrets, Firestore data, and reference YAMLs to ./migration-data/?" y; then
        warn "Skipping backup. Resources will be destroyed without local copies."
        if ! prompt_yn "Are you sure you want to skip backup?" n; then
            echo "Aborted at backup step."
            exit 0
        fi
        hr
        return 0
    fi

    local migration_dir="$REPO_ROOT/migration-data"
    mkdir -p "$migration_dir"/{secrets,firestore,refs}

    # Slack signing secret value
    say "Backing up slack-signing-secret value..."
    if gcloud secrets describe slack-signing-secret --project="$PROJECT_ID" >/dev/null 2>&1; then
        gcloud secrets versions access latest --secret=slack-signing-secret --project="$PROJECT_ID" \
            > "$migration_dir/secrets/slack-signing-secret.value"
        chmod 600 "$migration_dir/secrets/slack-signing-secret.value"
        ok "  Saved secrets/slack-signing-secret.value"
    else
        warn "  slack-signing-secret not found — skipping (use_slack=false?)"
    fi

    # Reference configs
    say "Snapshotting reference configs..."
    if gcloud run services describe the-forum --region="$REGION" --project="$PROJECT_ID" --format=yaml \
        > "$migration_dir/refs/cloud-run-the-forum.yaml" 2>/dev/null; then
        ok "  Saved refs/cloud-run-the-forum.yaml"
    else
        rm -f "$migration_dir/refs/cloud-run-the-forum.yaml"
        warn "  Cloud Run service 'the-forum' not found — skipping."
    fi

    if gcloud scheduler jobs describe scheduled-jobs-dispatcher --location="$REGION" --project="$PROJECT_ID" --format=yaml \
        > "$migration_dir/refs/scheduler-job.yaml" 2>/dev/null; then
        ok "  Saved refs/scheduler-job.yaml"
    else
        rm -f "$migration_dir/refs/scheduler-job.yaml"
        warn "  Scheduler job not found — skipping."
    fi

    gcloud projects get-iam-policy "$PROJECT_ID" --format=yaml \
        > "$migration_dir/refs/project-iam-policy.yaml"
    ok "  Saved refs/project-iam-policy.yaml"

    if [[ -s "$migration_dir/secrets/slack-signing-secret.value" ]]; then
        gcloud secrets get-iam-policy slack-signing-secret --project="$PROJECT_ID" --format=yaml \
            > "$migration_dir/refs/slack-signing-secret-iam.yaml"
        ok "  Saved refs/slack-signing-secret-iam.yaml"
    fi

    # Firestore export
    say "Exporting Firestore data..."
    if gcloud firestore databases describe --database='(default)' --project="$PROJECT_ID" >/dev/null 2>&1; then
        local backup_name
        backup_name="firestore-backup-$(date -u +%Y-%m-%d)"
        local staging_bucket="${PROJECT_ID}-staging"

        if ! gcloud storage buckets describe "gs://$staging_bucket" --project="$PROJECT_ID" >/dev/null 2>&1; then
            err "  Staging bucket gs://$staging_bucket not found — Firestore export needs a GCS path."
            echo "  Skipping Firestore backup. Restore the bucket and re-run if you need data."
        else
            local local_target="$migration_dir/firestore/$backup_name"
            if [[ -d "$local_target" ]]; then
                if ! prompt_yn "  $local_target already exists. Overwrite?" n; then
                    warn "  Keeping existing local backup; skipping Firestore export."
                else
                    rm -rf "$local_target"
                fi
            fi
            if [[ ! -d "$local_target" ]]; then
                gcloud firestore export "gs://$staging_bucket/$backup_name" \
                    --project="$PROJECT_ID" \
                    --collection-ids=agents,sessions,scheduled_jobs,users 2>&1 | tail -3
                say "  Downloading export to $local_target/..."
                gcloud storage cp --recursive "gs://$staging_bucket/$backup_name" "$migration_dir/firestore/" \
                    --project="$PROJECT_ID" 2>&1 | tail -3
                ok "  Saved firestore/$backup_name/"
            fi
        fi
    else
        warn "  Firestore database not found — skipping."
    fi

    ok "Backup complete: $migration_dir"
    hr
}

# --- Phase 4: Empty the staging bucket ---
phase_4_empty_staging() {
    say "Phase 4: Empty staging bucket (force_destroy=false in terraform)"
    local staging_bucket="${PROJECT_ID}-staging"
    if ! gcloud storage buckets describe "gs://$staging_bucket" --project="$PROJECT_ID" >/dev/null 2>&1; then
        ok "  Staging bucket not found — already gone."
        hr
        return 0
    fi
    if [[ -n "$(gcloud storage ls "gs://$staging_bucket/" 2>/dev/null)" ]]; then
        say "  Removing all objects from gs://$staging_bucket/..."
        gcloud storage rm --recursive "gs://$staging_bucket/**" --quiet 2>&1 | tail -3 || true
    fi
    ok "  Staging bucket emptied."
    hr
}

# --- Phase 5: Disable Firestore delete protection ---
phase_5_firestore_protection() {
    say "Phase 5: Disable Firestore delete protection"
    if ! gcloud firestore databases describe --database='(default)' --project="$PROJECT_ID" >/dev/null 2>&1; then
        ok "  Firestore database not found — already gone."
        hr
        return 0
    fi
    local state
    state=$(gcloud firestore databases describe --database='(default)' --project="$PROJECT_ID" \
        --format='value(deleteProtectionState)')
    if [[ "$state" == "DELETE_PROTECTION_ENABLED" ]]; then
        gcloud firestore databases update --database='(default)' --project="$PROJECT_ID" --no-delete-protection
        ok "  Delete protection disabled."
    else
        ok "  Delete protection already disabled."
    fi
    hr
}

# --- Phase 6: terraform destroy ---
phase_6_destroy() {
    say "Phase 6: terraform destroy"
    echo
    echo "${BOLD}${RED}This will permanently delete all infrastructure managed by terraform.${NC}"
    if ! prompt_yn "Run terraform destroy?" n; then
        echo "Aborted before destroy."
        if [[ -d "$REPO_ROOT/migration-data" ]]; then
            echo "Backups in $REPO_ROOT/migration-data/ are preserved."
        fi
        exit 0
    fi
    (cd "$REPO_ROOT/terraform" && terraform destroy -auto-approve)
    ok "Terraform destroy complete."

    destroy_backstop
    hr
}

# Backstop: check whether any terraform-managed resource that would block a
# re-install survived terraform destroy, and offer to clean it up via gcloud.
# Most commonly this catches Firestore databases when legacy state has
# deletion_policy=ABANDON, but it also covers Cloud Run, Scheduler,
# scheduler-sa, the slack-signing-secret container, and the two GCS buckets.
# Project IAM bindings, org policies, and API enablement are not checked
# because terraform destroy handles those reliably (or the resource has
# disable_on_destroy=false).
destroy_backstop() {
    say "Verifying terraform-managed resources are actually gone..."

    local survivors=()

    if gcloud run services describe the-forum --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
        survivors+=("cloud-run:the-forum")
    fi
    if gcloud scheduler jobs describe scheduled-jobs-dispatcher --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
        survivors+=("scheduler-job:scheduled-jobs-dispatcher")
    fi
    if gcloud iam service-accounts describe "scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com" --project="$PROJECT_ID" >/dev/null 2>&1; then
        survivors+=("service-account:scheduler-sa")
    fi
    if gcloud secrets describe slack-signing-secret --project="$PROJECT_ID" >/dev/null 2>&1; then
        survivors+=("secret:slack-signing-secret")
    fi
    for bucket in "${PROJECT_ID}-slack-files" "${PROJECT_ID}-staging"; do
        if gcloud storage buckets describe "gs://$bucket" --project="$PROJECT_ID" >/dev/null 2>&1; then
            survivors+=("bucket:$bucket")
        fi
    done
    if gcloud firestore databases describe --database='(default)' --project="$PROJECT_ID" >/dev/null 2>&1; then
        survivors+=("firestore-database:(default)")
    fi

    if [[ ${#survivors[@]} -eq 0 ]]; then
        ok "  All terraform-managed resources successfully destroyed."
        return 0
    fi

    warn "Found ${#survivors[@]} terraform-managed resource(s) that survived terraform destroy:"
    for s in "${survivors[@]}"; do
        echo "    - $s"
    done
    echo
    echo "  Most common cause: a resource's deletion_policy is ABANDON in state, so"
    echo "  terraform removed it from state without calling the GCP delete API."
    echo "  Without cleanup these will block the next install.sh run with 'already exists'."
    echo

    if ! prompt_yn "Delete these via gcloud now?" y; then
        warn "Skipped. Survivors will block re-install — clean up manually before retrying."
        return 0
    fi

    for s in "${survivors[@]}"; do
        local kind="${s%%:*}"
        local name="${s#*:}"
        case "$kind" in
            cloud-run)
                gcloud run services delete "$name" --region="$REGION" --project="$PROJECT_ID" --quiet
                ok "  Deleted Cloud Run service: $name"
                ;;
            scheduler-job)
                gcloud scheduler jobs delete "$name" --location="$REGION" --project="$PROJECT_ID" --quiet
                ok "  Deleted Cloud Scheduler job: $name"
                ;;
            service-account)
                gcloud iam service-accounts delete "${name}@${PROJECT_ID}.iam.gserviceaccount.com" --project="$PROJECT_ID" --quiet
                ok "  Deleted service account: $name"
                ;;
            secret)
                gcloud secrets delete "$name" --project="$PROJECT_ID" --quiet
                ok "  Deleted secret: $name"
                ;;
            bucket)
                # Empty bucket first (force destroy)
                gcloud storage rm --recursive "gs://$name/**" --quiet 2>&1 | tail -1 || true
                gcloud storage buckets delete "gs://$name" --project="$PROJECT_ID" --quiet
                ok "  Deleted bucket: $name"
                ;;
            firestore-database)
                local protection_state
                protection_state=$(gcloud firestore databases describe --database="$name" --project="$PROJECT_ID" \
                    --format='value(deleteProtectionState)')
                if [[ "$protection_state" == "DELETE_PROTECTION_ENABLED" ]]; then
                    gcloud firestore databases update --database="$name" --project="$PROJECT_ID" --no-delete-protection
                fi
                gcloud firestore databases delete --database="$name" --project="$PROJECT_ID" --quiet
                ok "  Deleted Firestore database: $name"
                ;;
        esac
    done
}

# --- Phase 7: Optional gcr.io image cleanup ---
phase_7_gcr_cleanup() {
    say "Phase 7: Container image cleanup"
    local image_path="gcr.io/$PROJECT_ID/the-forum"
    local image_count
    image_count=$(gcloud container images list-tags "$image_path" --project="$PROJECT_ID" \
        --format='value(digest)' 2>/dev/null | wc -l)
    if [[ "$image_count" -eq 0 ]]; then
        ok "  No $image_path images found."
        hr
        return 0
    fi
    echo "Found $image_count tagged versions of $image_path."
    if prompt_yn "Delete all of them? (Choosing No keeps them for rollback.)" n; then
        while read -r digest; do
            [[ -z "$digest" ]] && continue
            gcloud container images delete "$image_path@$digest" \
                --project="$PROJECT_ID" --quiet --force-delete-tags 2>&1 | tail -1 || true
        done < <(gcloud container images list-tags "$image_path" --project="$PROJECT_ID" --format='value(digest)')
        ok "  All images deleted."
    else
        ok "  Keeping images at $image_path."
    fi
    hr
}

# --- Phase 8: Optional state bucket cleanup ---
phase_8_state_bucket() {
    say "Phase 8: Terraform state bucket"
    local state_bucket="${PROJECT_ID}-terraform-state"
    if ! gcloud storage buckets describe "gs://$state_bucket" --project="$PROJECT_ID" >/dev/null 2>&1; then
        ok "  State bucket not found — already gone."
        hr
        return 0
    fi
    echo "Keeping the state bucket gs://$state_bucket makes future re-installs faster"
    echo "(install.sh will reuse it instead of bootstrapping a new one)."
    if prompt_yn "Delete the state bucket?" n; then
        gcloud storage rm --recursive "gs://$state_bucket/**" --quiet 2>&1 | tail -3 || true
        gcloud storage buckets delete "gs://$state_bucket" --project="$PROJECT_ID" --quiet
        ok "  State bucket deleted."
    else
        ok "  Keeping state bucket."
    fi
    hr
}

# --- Phase 9: Summary ---
phase_9_summary() {
    cat <<EOF
${BOLD}=== Uninstall complete ===${NC}

Local artifacts kept (delete manually for a clean reset):
  - $REPO_ROOT/.env
  - $REPO_ROOT/terraform/terraform.tfvars
  - $REPO_ROOT/terraform/.terraform/
  - $REPO_ROOT/terraform/.terraform.lock.hcl

EOF
    if [[ -d "$REPO_ROOT/migration-data" ]]; then
        cat <<EOF
${BOLD}Backups${NC} are in: $REPO_ROOT/migration-data/
  Re-import on next install: run ./scripts/install.sh and answer 'yes'
  at the migration prompt; point it at this folder.

EOF
    fi
}

# --- Main ---
main() {
    phase_1_announce
    phase_2_detect
    phase_3_backup
    phase_4_empty_staging
    phase_5_firestore_protection
    phase_6_destroy
    phase_7_gcr_cleanup
    phase_8_state_bucket
    phase_9_summary
}

main "$@"
