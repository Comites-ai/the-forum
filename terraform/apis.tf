# Enable Required GCP APIs

resource "google_project_service" "firestore" {
  service            = "firestore.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "aiplatform" {
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "run" {
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "cloudbuild" {
  service            = "cloudbuild.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "cloudscheduler" {
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "chat" {
  service            = "chat.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "storage" {
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "secretmanager" {
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "cloudtrace" {
  service            = "cloudtrace.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "logging" {
  service            = "logging.googleapis.com"
  disable_on_destroy = false
}

# Compute Engine API — needed only by the discord-worker VM. Gated on
# var.use_discord so a default-config terraform apply does not enable
# this API on projects that don't want it. API enablement itself is free
# but is still a project-level mutation we shouldn't perform implicitly.
resource "google_project_service" "compute" {
  count              = var.use_discord ? 1 : 0
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

# Artifact Registry API — needed only by the discord-worker container
# image repo. Gated on var.use_discord for the same reason as compute.
resource "google_project_service" "artifactregistry" {
  count              = var.use_discord ? 1 : 0
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}
