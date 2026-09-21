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

# Conversation log retention (PLAT-43). The Forum writes every message it
# relays to conversations/{agent}__{user}/messages with an expires_at of
# created_at + 7 days; this TTL policy is the only thing that deletes them.
# `collection` is a collection-group id, so it covers every `messages`
# subcollection under every conversation. Firestore removes expired
# documents within roughly 24 hours of expires_at.
resource "google_firestore_field" "messages_ttl" {
  project    = var.project_id
  database   = google_firestore_database.database.name
  collection = "messages"
  field      = "expires_at"

  ttl_config {}

  depends_on = [google_firestore_database.database]
}

# Delivery receipts (PLAT-51). query_agent writes one record per call to
# a2a_sessions/{caller}__{target}__{user}/a2a_queries with an expires_at of
# sent_at + 7 days, so a caller can find out what became of a call whose
# result it never saw. Same shape as the conversation log above: the TTL
# policy is the only thing that deletes them.
resource "google_firestore_field" "a2a_queries_ttl" {
  project    = var.project_id
  database   = google_firestore_database.database.name
  collection = "a2a_queries"
  field      = "expires_at"

  ttl_config {}

  depends_on = [google_firestore_database.database]
}
