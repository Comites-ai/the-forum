# Cloud Scheduler Configuration

resource "google_cloud_scheduler_job" "scheduled_jobs_dispatcher" {
  name             = var.scheduler_job_name
  description      = "Triggers scheduled job processing every minute"
  schedule         = var.scheduler_cron_schedule
  time_zone        = "UTC"
  attempt_deadline = "320s"
  region           = var.region

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.forum.uri}/api/v1/scheduled-jobs/process"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [
    google_project_service.cloudscheduler,
    google_cloud_run_v2_service.forum,
    google_service_account.scheduler
  ]
}
