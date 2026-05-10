# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Platform-agnostic event models for unified message processing."""
from typing import Optional, Any
from pydantic import BaseModel, Field


class PlatformEvent(BaseModel):
    """
    Unified event model that abstracts platform-specific message events.

    This model normalizes events from different platforms (Slack, Google Chat, etc.)
    into a common format for processing.
    """
    platform: str = Field(
        ...,
        description="Platform name (slack, google_chat)"
    )
    user_id: str = Field(
        ...,
        description="Platform-specific user identifier"
    )
    user_email: Optional[str] = Field(
        default=None,
        description="User's email (if available, used for auto-linking)"
    )
    message_text: str = Field(
        ...,
        description="Message text content"
    )
    space_id: str = Field(
        ...,
        description="Conversation/channel/space identifier (Google Chat space ID or Slack channel ID)"
    )
    files: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "File attachments. Each dict is expected to carry canonical keys "
            "'mimetype' (str) and 'download_ref' (str, opaque reference the "
            "owning connector knows how to fetch). Connectors may include "
            "extra keys (size, name, etc.) for diagnostics."
        )
    )
    media_group_id: Optional[str] = Field(
        default=None,
        description=(
            "Set by Telegram when this event is part of a multi-photo album. "
            "Treated by the processor as a 'multiple images' signal even when "
            "len(files) == 1, since album photos arrive as separate webhooks."
        )
    )
    raw_event: dict[str, Any] = Field(
        ...,
        description="Original platform event data for reference"
    )

    model_config = {"frozen": True}  # Immutable after creation
