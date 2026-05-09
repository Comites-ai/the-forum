# Firestore Database Configuration

resource "google_firestore_database" "database" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Firestore requires the API to be enabled first
  depends_on = [
    google_project_service.firestore
  ]
}
