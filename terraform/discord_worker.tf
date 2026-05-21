# =============================================================================
# Discord Worker VM (multi-tenant)
#
# Discord cannot deliver DMs over HTTP webhooks — only over a long-lived
# Gateway WebSocket. The Forum's Cloud Run service is request-driven and
# scales to zero, which is the wrong fit for a long-lived socket. So when
# var.use_discord is true, we provision a small Compute Engine VM that
# holds Gateway connections open — ONE connection per Discord-enabled
# agent — and forwards each DM to the Forum's
# /api/v1/discord/events/{agent_id} endpoint.
#
# The worker is multi-tenant: it queries the Forum's Firestore `agents`
# collection on startup (and on a refresh interval) for any agent that
# has a `discord` platform block with `enabled: true`, then opens one
# Gateway connection per such agent in a single Python process. Bot
# tokens live in the AGENTS' projects, not the Forum's project. The
# agent-project terraform template (docs/terraform-templates/agent-project)
# creates each agent's `discord-bot-token` secret and grants the Forum's
# worker SA cross-project secretAccessor.
#
# COST NOTE:
#   The default machine_type (e2-micro) in us-central1, us-west1, or
#   us-east1 is included in GCP's Always Free tier — one VM per billing
#   account. If your free-tier e2-micro is already in use, or you pick a
#   region outside that list, expect ~$6-7/month for the instance. Verify
#   your billing console before applying.
#
# OS PATCHING:
#   The VM runs Container-Optimized OS (cos-stable) with automatic updates
#   enabled, so the host OS patches itself. The discord-worker container
#   image, however, is pinned and must be rebuilt and redeployed manually
#   when discord.py, the Python base image, or any other dependency ships
#   a security fix. See docs/DISCORD_WORKER.md for the redeploy runbook;
#   review and rebuild quarterly or sooner if a CVE is reported.
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

# Dedicated service account for the worker VM. Each agent's Firestore
# document sets `discord_worker_service_account` to this SA's email so
# the Forum's /api/v1/discord/events/{agent_id} handler will accept
# events forwarded by this worker. With a single multi-tenant worker,
# the same email goes in every Discord-enabled agent's document.
resource "google_service_account" "discord_worker" {
  count        = var.use_discord ? 1 : 0
  account_id   = "discord-worker"
  display_name = "Discord Gateway Worker"
  description  = "Holds Discord Gateway WebSockets and forwards DMs to the Forum."

  depends_on = [google_project_service.compute[0]]
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

# The worker calls Cloud Run with an OIDC bearer token. We grant invoker
# explicitly so the audience check on the token passes cleanly even if
# the service ever moves to restricted ingress.
resource "google_cloud_run_v2_service_iam_member" "discord_worker_invoker" {
  count    = var.use_discord ? 1 : 0
  location = google_cloud_run_v2_service.forum.location
  name     = google_cloud_run_v2_service.forum.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.discord_worker[0].email}"
}

# The worker queries the agents collection in Firestore to discover which
# bots to maintain Gateway connections for. `datastore.user` is the right
# role for Firestore Native (Cloud Datastore is the legacy name).
resource "google_project_iam_member" "discord_worker_firestore" {
  count   = var.use_discord ? 1 : 0
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.discord_worker[0].email}"
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
    # container automatically. No per-agent env vars here — the worker
    # discovers its bot list from Firestore at runtime.
    gce-container-declaration = yamlencode({
      spec = {
        containers = [{
          name  = "discord-worker"
          image = var.discord_worker_image
          env = [
            { name = "FORUM_URL", value = google_cloud_run_v2_service.forum.uri },
            { name = "FIRESTORE_PROJECT_ID", value = var.project_id },
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
      condition     = length(var.discord_worker_image) > 0
      error_message = "When use_discord is true, discord_worker_image must be set. Build the image with `gcloud builds submit discord-worker --tag=...` and pass the resulting URL."
    }
  }

  depends_on = [
    google_project_service.compute[0],
    google_project_iam_member.discord_worker_firestore[0],
  ]
}
