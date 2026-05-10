# Firestore Database Configuration

resource "google_firestore_database" "database" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Without this the google provider defaults to ABANDON for default
  # databases, meaning 'terraform destroy' silently removes the database
  # from state but never calls the GCP delete API. The database survives,
  # and the next 'terraform apply' fails with "Database already exists".
  # DELETE makes destroy actually delete.
  delete_protection_state = "DELETE_PROTECTION_DISABLED"
  deletion_policy         = "DELETE"

  # Firestore requires the API to be enabled first
  depends_on = [
    google_project_service.firestore
  ]
}
