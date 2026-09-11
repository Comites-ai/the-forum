# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Application configuration using Pydantic Settings."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration with type-safe environment variables."""

    # Application settings
    app_name: str = "The Forum by Comites.ai"
    environment: str = "development"
    log_level: str = "INFO"

    # Google Cloud Platform
    gcp_project_id: str
    gcp_location: str = "us-central1"

    # Firestore
    firestore_agents_collection: str = "agents"
    firestore_sessions_collection: str = "sessions"
    firestore_scheduled_jobs_collection: str = "scheduled_jobs"

    # Session management
    # Sessions expire after this many minutes of inactivity
    # A new Vertex AI session will be created after expiry
    session_timeout_minutes: int = 180  # 3 hours

    # Session healing (app/services/session_healer.py).
    #
    # A run cut off between a function_call and its result leaves the session
    # unusable for Anthropic-backed agents. Agents built from the current
    # Agent-Template repair this themselves; healing here is the safety net
    # for the ones that don't. It is off by default because it makes the
    # Forum write into an agent's own memory, which no operator should get
    # without asking for it.
    heal_orphaned_tool_calls: bool = False
    # How long an unanswered tool call must sit at the tail of a session
    # before we accept that nothing is coming. Below this we assume the tool
    # is merely slow and leave the session alone.
    heal_grace_seconds: int = 120
    # How long an in-flight run marker must sit there before we conclude the
    # run is never coming back. Deliberately longer than the tail grace and
    # longer than any run can survive — Cloud Run's own request timeout is
    # 300s — because the cost of guessing wrong here is high: a run still
    # genuinely in progress would be treated as abandoned, and a tool that
    # is simply slow could have its result written for it.
    cut_off_after_seconds: int = 600

    # Fallback IANA timezone for localizing message timestamps when a user
    # has no default_timezone set (and the platform doesn't report one).
    # America/New_York (not the fixed-offset "EST") so DST is handled.
    default_user_timezone: str = "America/New_York"

    # Slack (comma-separated list to support multiple Slack apps)
    slack_signing_secret: str

    @property
    def slack_signing_secrets(self) -> list[str]:
        """Parse SLACK_SIGNING_SECRET as a comma-separated list."""
        return [s.strip() for s in self.slack_signing_secret.split(",") if s.strip()]

    # Cloud Scheduler (for scheduled jobs)
    cloud_run_url: str = ""  # For OIDC audience verification (e.g., https://service-xxx.run.app)
    cloud_scheduler_location: str = "us-central1"
    cloud_scheduler_service_account: str = ""  # scheduler-sa@PROJECT.iam.gserviceaccount.com
    scheduled_job_lock_timeout_seconds: int = 300  # 5 minutes

    # GCS Configuration (for file uploads)
    # When set, files are uploaded to GCS instead of being base64 encoded
    gcs_bucket_name: str = ""  # Empty = GCS disabled, use base64 fallback
    gcs_file_prefix: str = "slack-files"  # Prefix for uploaded objects

    @property
    def gcs_enabled(self) -> bool:
        """Check if GCS file upload is configured."""
        return bool(self.gcs_bucket_name)

    # File intake limits.
    #
    # allowed_image_mime_types is the *default* capability: it applies to
    # every agent that declares no accepted_file_types of its own, which
    # keeps pre-existing agents on exactly their old behavior.
    max_image_size_mb: int = 20
    allowed_image_mime_types: list[str] = [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/heif",
    ]

    # Non-image attachments (PDFs and the like) are capped separately —
    # a scanned multi-page invoice is legitimately larger than a photo,
    # and the two limits have no reason to move together.
    max_document_size_mb: int = 20

    # API settings
    api_v1_prefix: str = "/api/v1"

    # Admin UI (Google OAuth, mounted at /admin)
    # When OAuth credentials are blank, the admin UI is disabled entirely
    # and the /admin paths simply 404. This keeps existing deployments
    # unaffected until an operator opts in.
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_redirect_uri: str = ""
    session_secret: str = ""
    admin_required_role: str = "roles/owner"
    # Cloud Run service name used to scope Cloud Logging queries from the
    # admin UI. Defaults to "the-forum" because that's the deployed name in
    # the bundled terraform; override per environment if it differs.
    cloud_run_service_name: str = "the-forum"

    @property
    def admin_ui_enabled(self) -> bool:
        """Whether the admin UI should be mounted at /admin."""
        return bool(
            self.oauth_client_id
            and self.oauth_client_secret
            and self.oauth_redirect_uri
            and self.session_secret
        )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    """
    Cached settings instance - loaded once per app lifecycle.

    Returns:
        Settings: Application configuration
    """
    return Settings()
