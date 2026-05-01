"""Agent configuration model."""
from typing import Optional
from pydantic import BaseModel, Field, model_validator


class AgentPlatformConfig(BaseModel):
    """
    Platform-specific configuration for an agent.

    Each platform has its own authentication and configuration requirements.
    """
    platform: str = Field(..., description="Platform name (slack, google_chat, telegram)")
    enabled: bool = Field(default=True, description="Whether this platform is active")

    # Slack-specific fields
    slack_bot_id: Optional[str] = Field(default=None, description="Slack bot user ID (U...)")
    slack_bot_token: Optional[str] = Field(
        default=None,
        description="[DEPRECATED] Direct Slack Bot OAuth Token - use slack_bot_token_secret instead"
    )
    slack_bot_token_secret: Optional[str] = Field(
        default=None,
        description="Secret Manager secret name for Slack bot token (e.g., 'my-agent-slack-token')"
    )
    slack_bot_token_project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID where the Slack bot token secret is stored"
    )

    # Google Chat-specific fields
    google_chat_service_account_secret: Optional[str] = Field(
        default=None,
        description="Secret Manager secret name for service account (e.g., 'growth-coach-credentials')"
    )
    google_chat_project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID where the Google Chat service account secret is stored"
    )
    google_chat_bot_name: Optional[str] = Field(
        default=None,
        description="Google Chat bot resource name"
    )

    # Telegram-specific fields
    telegram_bot_token: Optional[str] = Field(
        default=None,
        description="Direct Telegram bot token from BotFather (use telegram_bot_token_secret instead for production)"
    )
    telegram_bot_token_secret: Optional[str] = Field(
        default=None,
        description="Secret Manager secret name for Telegram bot token (e.g., 'my-agent-telegram-token')"
    )
    telegram_bot_token_project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID where the Telegram bot token secret is stored"
    )
    telegram_webhook_secret: Optional[str] = Field(
        default=None,
        description="Secret token for Telegram webhook verification (X-Telegram-Bot-Api-Secret-Token)"
    )


class Agent(BaseModel):
    """
    Agent configuration stored in Firestore.

    Supports multiple messaging platforms (Slack, Google Chat, etc.).
    Maintains backward compatibility with existing Slack-only configuration.
    """

    id: Optional[str] = Field(default=None, description="Firestore document ID")
    vertex_ai_agent_id: str = Field(..., description="Vertex AI agent resource name")
    display_name: str = Field(..., description="Human-readable agent name")

    # Legacy Slack fields (for backward compatibility)
    slack_bot_token: Optional[str] = Field(default=None, description="[DEPRECATED] Use platforms instead")
    slack_bot_id: Optional[str] = Field(default=None, description="[DEPRECATED] Use platforms instead")

    # New multi-platform configuration
    platforms: Optional[list[AgentPlatformConfig]] = Field(
        default=None,
        description="Platform-specific configurations"
    )

    model_config = {"frozen": False}  # Mutable for backward compat migration

    @model_validator(mode='after')
    def ensure_platforms(self):
        """
        Ensure platforms list exists for backward compatibility.

        If agent has legacy slack_bot_token/slack_bot_id but no platforms list,
        automatically create a Slack platform config from the legacy fields.
        """
        if self.platforms is None:
            self.platforms = []

        # Backward compatibility: migrate legacy Slack fields to platforms
        if self.slack_bot_token and self.slack_bot_id and not self._has_slack_platform():
            slack_config = AgentPlatformConfig(
                platform="slack",
                enabled=True,
                slack_bot_id=self.slack_bot_id,
                slack_bot_token=self.slack_bot_token
            )
            self.platforms.append(slack_config)

        return self

    def _has_slack_platform(self) -> bool:
        """Check if platforms list already has a Slack configuration."""
        if not self.platforms:
            return False
        return any(p.platform == "slack" for p in self.platforms)

    def get_platform_config(self, platform: str) -> Optional[AgentPlatformConfig]:
        """
        Get platform-specific configuration.

        Args:
            platform: Platform name (e.g., "slack", "google_chat")

        Returns:
            Platform config if found and enabled, None otherwise
        """
        if not self.platforms:
            return None

        for config in self.platforms:
            if config.platform == platform and config.enabled:
                return config
        return None

    def get_slack_config(self) -> Optional[AgentPlatformConfig]:
        """Get Slack platform configuration (convenience method)."""
        return self.get_platform_config("slack")

    def get_google_chat_config(self) -> Optional[AgentPlatformConfig]:
        """Get Google Chat platform configuration (convenience method)."""
        return self.get_platform_config("google_chat")

    def get_telegram_config(self) -> Optional[AgentPlatformConfig]:
        """Get Telegram platform configuration (convenience method)."""
        return self.get_platform_config("telegram")
