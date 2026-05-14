# Cloud Run Service Configuration

# Wait for the slack-signing-secret IAM binding to propagate before Cloud
# Run validates the bound secret. GCP IAM is eventually consistent — even
# after the binding API call returns, Cloud Run's revision-creation auth
# check can take 30+ seconds to see it. Without this delay, fresh installs
# fail with "Permission denied on secret ... for Revision service account".
resource "time_sleep" "wait_for_slack_secret_iam" {
  count           = var.use_slack ? 1 : 0
  depends_on      = [google_secret_manager_secret_iam_member.compute_slack_signing_secret]
  create_duration = "30s"
}

# Same eventual-consistency wait for the admin UI secrets when they exist.
resource "time_sleep" "wait_for_admin_secret_iam" {
  count = var.enable_admin_ui ? 1 : 0
  depends_on = [
    google_secret_manager_secret_iam_member.compute_oauth_client_id,
    google_secret_manager_secret_iam_member.compute_oauth_client_secret,
    google_secret_manager_secret_iam_member.compute_admin_session_secret,
  ]
  create_duration = "30s"
}

resource "google_cloud_run_v2_service" "forum" {
  name     = var.cloud_run_service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    # Use default compute service account
    service_account = local.default_compute_sa

    containers {
      # Public hello-world placeholder so the first terraform apply succeeds
      # before any real image has been pushed. Cloud Build (deploy_forum.sh)
      # then deploys the real image; subsequent applies ignore the image
      # field via the lifecycle.ignore_changes block below.
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      # Environment variables
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "GCP_LOCATION"
        value = var.region
      }

      env {
        name  = "GCS_BUCKET_NAME"
        value = google_storage_bucket.slack_files.name
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      # Reference secrets — Slack binding only when var.use_slack is true.
      # Cloudbuild's --set-secrets in cloudbuild.yaml takes over after the
      # initial create (see lifecycle.ignore_changes below), so this is the
      # bootstrap value only.
      dynamic "env" {
        for_each = var.use_slack ? [1] : []
        content {
          name = "SLACK_SIGNING_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.slack_signing_secret[0].secret_id
              version = "latest"
            }
          }
        }
      }

      # Admin UI secrets and config — bound only when enable_admin_ui is true.
      dynamic "env" {
        for_each = var.enable_admin_ui ? [1] : []
        content {
          name = "OAUTH_CLIENT_ID"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.oauth_client_id[0].secret_id
              version = "latest"
            }
          }
        }
      }
      dynamic "env" {
        for_each = var.enable_admin_ui ? [1] : []
        content {
          name = "OAUTH_CLIENT_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.oauth_client_secret[0].secret_id
              version = "latest"
            }
          }
        }
      }
      dynamic "env" {
        for_each = var.enable_admin_ui ? [1] : []
        content {
          name = "SESSION_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.admin_session_secret[0].secret_id
              version = "latest"
            }
          }
        }
      }
      # OAUTH_REDIRECT_URI is intentionally NOT bound here: the Cloud Run
      # service's own URL is only known after creation, so a self-reference
      # would create a terraform cycle. After the first `terraform apply`,
      # run:
      #
      #   gcloud run services update <service> \
      #     --region <region> \
      #     --update-env-vars OAUTH_REDIRECT_URI=<service-url>/admin/auth/callback
      #
      # Until then `settings.admin_ui_enabled` will be False and the admin
      # routes 404 — which is the safe degraded state. See
      # docs/ADMIN_UI.md for the full bring-up sequence.
      dynamic "env" {
        for_each = var.enable_admin_ui ? [1] : []
        content {
          name  = "ADMIN_REQUIRED_ROLE"
          value = var.admin_required_role
        }
      }
      dynamic "env" {
        for_each = var.enable_admin_ui ? [1] : []
        content {
          name  = "CLOUD_RUN_SERVICE_NAME"
          value = var.cloud_run_service_name
        }
      }

      # Resource limits
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      # No explicit startup_probe: Cloud Run's default TCP probe gives a
      # ~4-minute boot budget, which fits the cold-start cost of importing
      # vertexai + google-cloud-aiplatform + firestore + slack-sdk + mcp on
      # 1 vCPU. The previous explicit HTTP probe to /health (period=3,
      # failure_threshold=3 → ~9s budget) consistently killed fresh
      # revisions before uvicorn finished importing app.main.
    }

    # Scaling configuration
    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
  }

  # Allow unauthenticated requests (webhooks from Slack/Google Chat)
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_service.run,
    google_storage_bucket.slack_files,
    google_secret_manager_secret.slack_signing_secret,
    time_sleep.wait_for_slack_secret_iam,
    time_sleep.wait_for_admin_secret_iam,
  ]

  # Ignore changes made outside Terraform (e.g., via cloudbuild.yaml or manual deployment)
  lifecycle {
    ignore_changes = [
      template[0].containers[0].env,
      template[0].scaling,
      template[0].containers[0].resources,
      template[0].containers[0].image,
      template[0].timeout,
      template[0].max_instance_request_concurrency,
    ]
  }
}

# Allow unauthenticated invocations
resource "google_cloud_run_service_iam_member" "noauth" {
  service  = google_cloud_run_v2_service.forum.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
