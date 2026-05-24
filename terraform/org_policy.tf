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
# Allow Reasoning Engines (and other resources) in this project to be
# attached to service accounts from other projects. The Agent Template
# (https://github.com/Comites-ai/Agent-Template) creates a per-agent SA
# in each agent's own GCP project, then deploys the Reasoning Engine
# into the Forum project with --service_account pointing at that
# cross-project SA. The default org policy
# `constraints/iam.disableCrossProjectServiceAccountUsage` (enforced
# by default on orgs created since Sep 2024) blocks this attachment;
# without the override, every agent's deploy succeeds at create time
# but every metadata-server token request at runtime returns 500.
#
# This override is one-side-of-two: the agent's own terraform handles
# the matching override on the SA's home project. Both sides are
# required because the constraint enforces on both the SA's home
# project AND the project where the SA is being attached.
#
# Why it has to live here, not in each agent's terraform: every agent
# deploying to the Forum would otherwise need its own terraform apply
# to reach into the Forum project and set this policy — which means
# every agent operator would need roles/orgpolicy.policyAdmin on the
# Forum, and any single agent's terraform destroy would revert the
# policy and break every other agent. The Forum operator setting it
# once, declaratively, is the only sane shape.
resource "google_project_organization_policy" "allow_cross_project_sa_runtime" {
  project    = var.project_id
  constraint = "constraints/iam.disableCrossProjectServiceAccountUsage"

  boolean_policy {
    enforced = false
  }
}

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
