# =============================================================================
# Discord Worker VM
#
# Discord cannot deliver DMs over HTTP webhooks — only over a Gateway
# WebSocket. The Forum's Cloud Run service is request-driven and scales to
# zero, which is the wrong fit for a long-lived socket. So when
# var.use_discord is true, we provision a small Compute Engine VM that
# holds the Gateway connection open and forwards each DM to the Forum's
# /api/v1/discord/events/{agent_id} endpoint.
#
# COST NOTE:
#   The default machine_type (e2-micro) in us-central1, us-west1, or
#   us-east1 is included in GCP's Always Free tier — one VM per billing
#   account. If your free-tier e2-micro is already in use, or you pick a
#   region outside that list, expect ~$6-7/month for the instance plus a
#   few cents for the secret. Verify your billing console before applying.
#
# OS PATCHING:
#   The VM runs Container-Optimized OS (cos-stable) with automatic updates
#   enabled, so the host OS patches itself. The discord-worker container
#   image, however, is pinned and must be rebuilt and redeployed manually
#   when discord.py, the Python base image, or any other dependency ships
#   a security fix. See docs/DISCORD_WORKER.md for the redeploy runbook;
#   review and rebuild quarterly or sooner if a CVE is reported.
#
# ROUTING NOTE:
#   One VM per agent. The agent ID this worker forwards events for is
#   baked in at boot via the AGENT_ID env var, mirroring how each Telegram
#   bot gets its own /api/v1/telegram/events/{agent_id} webhook URL.
# =============================================================================

# Artifact Registry repo that holds the worker container image. Cloud
# Build pushes here when you run
#   gcloud builds submit discord-worker --tag=us-central1-docker.pkg.dev/<project>/discord-worker/worker:latest
# and the VM pulls from here at boot. Kept separate from
# cloud-run-source-deploy (which holds the Forum image) so the two have
# independent lifecycles and access policies.
resource "google_artifact_registry_repository" "discord_worker" {
  count         = var.use_discord ? 1 : 0
  location      = var.region
  repository_id = "discord-worker"
  format        = "DOCKER"
  description   = "Container images for the Discord Gateway worker VM"

  depends_on = [google_project_service.artifactregistry[0]]
}

# Dedicated service account for the worker VM. The Forum's
# /api/v1/discord/events/{agent_id} handler verifies the OIDC token the
# worker presents and matches its email against the agent's
# discord_worker_service_account field, so this email is exactly what you
# put in the agent's Firestore document.
resource "google_service_account" "discord_worker" {
  count        = var.use_discord ? 1 : 0
  account_id   = "discord-worker"
  display_name = "Discord Gateway Worker"
  description  = "Holds the Discord Gateway WebSocket and forwards DMs to the Forum."

  depends_on = [google_project_service.compute[0]]
}

# The worker reads the bot token from Secret Manager at startup.
resource "google_secret_manager_secret_iam_member" "discord_worker_token" {
  count     = var.use_discord ? 1 : 0
  secret_id = google_secret_manager_secret.discord_bot_token[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.discord_worker[0].email}"
}

# The VM pulls the worker container image from this Artifact Registry repo
# at boot. COS uses the attached service account for the pull.
resource "google_artifact_registry_repository_iam_member" "discord_worker_reader" {
  count      = var.use_discord ? 1 : 0
  project    = var.project_id
  location   = google_artifact_registry_repository.discord_worker[0].location
  repository = google_artifact_registry_repository.discord_worker[0].name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.discord_worker[0].email}"
}

# The worker calls Cloud Run with an OIDC bearer token. The Cloud Run
# service is invocable by all-users for the public webhook routes, but
# we still grant invoker explicitly so the audience check on the token
# passes cleanly in restricted-ingress configurations.
resource "google_cloud_run_v2_service_iam_member" "discord_worker_invoker" {
  count    = var.use_discord ? 1 : 0
  location = google_cloud_run_v2_service.forum.location
  name     = google_cloud_run_v2_service.forum.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.discord_worker[0].email}"
}

# Standard observability roles so the VM and its container can write logs
# and metrics under their own identity.
resource "google_project_iam_member" "discord_worker_logging" {
  count   = var.use_discord ? 1 : 0
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.discord_worker[0].email}"
}

resource "google_project_iam_member" "discord_worker_monitoring" {
  count   = var.use_discord ? 1 : 0
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.discord_worker[0].email}"
}

# The VM itself. Container-Optimized OS (COS) pulls and runs the worker
# image declared in gce-container-declaration. COS receives automatic
# security updates from Google for the OS; the container image is pinned
# and is YOUR responsibility to rebuild — see docs/DISCORD_WORKER.md.
resource "google_compute_instance" "discord_worker" {
  count        = var.use_discord ? 1 : 0
  name         = "discord-worker"
  machine_type = var.discord_worker_machine_type
  zone         = var.discord_worker_zone

  # COS auto-updates the host OS; auto-restart and live migration keep the
  # worker available through maintenance events.
  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
    preemptible         = false
  }

  boot_disk {
    initialize_params {
      # cos-stable: actively maintained Container-Optimized OS image
      # family. New images are released by Google ~monthly and this
      # config picks the latest at instance-creation time.
      image = "cos-cloud/cos-stable"
      size  = 10
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"
    access_config {
      # Ephemeral public IP. The worker needs outbound HTTPS to
      # discord.com (Gateway) and *.run.app (Forum). It does NOT accept
      # inbound traffic; if you want to lock that down further, attach
      # a firewall tag and a deny-all-ingress rule.
    }
  }

  metadata = {
    # COS reads gce-container-declaration and runs the resulting
    # container automatically. The container_vm.spec object below is
    # the same shape `gcloud compute instances create-with-container`
    # would produce.
    gce-container-declaration = yamlencode({
      spec = {
        containers = [{
          name  = "discord-worker"
          image = var.discord_worker_image
          env = [
            { name = "FORUM_URL", value = google_cloud_run_v2_service.forum.uri },
            { name = "AGENT_ID", value = var.discord_agent_id },
            { name = "DISCORD_BOT_TOKEN_SECRET", value = google_secret_manager_secret.discord_bot_token[0].secret_id },
            { name = "DISCORD_BOT_TOKEN_PROJECT_ID", value = var.project_id },
            { name = "LOG_LEVEL", value = "INFO" },
          ]
          stdin = false
          tty   = false
        }]
        restartPolicy = "Always"
      }
    })

    google-logging-enabled    = "true"
    google-monitoring-enabled = "true"
  }

  service_account {
    email = google_service_account.discord_worker[0].email
    # cloud-platform is the standard scope for service-account-driven
    # auth; the actual capabilities are constrained by the SA's IAM roles.
    scopes = ["cloud-platform"]
  }

  lifecycle {
    precondition {
      condition     = length(var.discord_agent_id) > 0
      error_message = "When use_discord is true, discord_agent_id must be set to the Firestore agent document ID the worker will forward events to."
    }
    precondition {
      condition     = length(var.discord_worker_image) > 0
      error_message = "When use_discord is true, discord_worker_image must be set. Build the image with `gcloud builds submit discord-worker --tag=...` and pass the resulting URL."
    }
    # Note: we do NOT precondition on the bot token's secret VALUE — terraform
    # no longer owns the value. If the secret is empty at boot, the worker
    # will fetch it, fail to log into Discord, and crash-loop until you run
    # `gcloud secrets versions add discord-bot-token --data-file=-` to
    # populate it. See docs/DISCORD_WORKER.md.
  }

  depends_on = [
    google_project_service.compute[0],
    google_secret_manager_secret.discord_bot_token[0],
    google_secret_manager_secret_iam_member.discord_worker_token[0],
  ]
}
