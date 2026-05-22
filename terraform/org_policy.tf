# Organization policy to allow public access to Cloud Run services
# This overrides the organization-level domain restriction for this project

resource "google_project_organization_policy" "domain_restriction_override" {
  project    = var.project_id
  constraint = "constraints/iam.allowedPolicyMemberDomains"

  list_policy {
    allow {
      all = true
    }
  }
}

# Targeted exception to constraints/compute.vmExternalIpAccess for the
# discord-worker VM only. The default org policy denies external IPs on
# every VM in this project; the worker needs outbound HTTPS to
# discord.com (Gateway WSS) and *.run.app (Forum), so we allow exactly
# one named instance to have an external IP. Any future VM in this
# project remains subject to the deny — to add another exception you'd
# need to extend this allow list.
resource "google_project_organization_policy" "discord_worker_external_ip" {
  count      = var.use_discord ? 1 : 0
  project    = var.project_id
  constraint = "constraints/compute.vmExternalIpAccess"

  list_policy {
    allow {
      values = [
        "projects/${var.project_id}/zones/${var.discord_worker_zone}/instances/discord-worker",
      ]
    }
  }
}
