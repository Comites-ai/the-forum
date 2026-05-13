# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""User identity model for multi-platform support."""
from datetime import datetime, UTC
from typing import Optional
from pydantic import BaseModel, Field


class PlatformIdentity(BaseModel):
    """
    Represents a user's identity on a specific platform.

    A single user can have multiple platform identities (e.g., Slack + Google Chat).
    """
    platform: str = Field(..., description="Platform name (slack, google_chat)")
    platform_user_id: str = Field(..., description="Platform-specific user ID")
    linked_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this identity was linked"
    )
    display_name: Optional[str] = Field(
        default=None,
        description="User's display name on this platform"
    )


class User(BaseModel):
    """
    Unified user identity across multiple messaging platforms.

    Users can have multiple platform identities (Slack, Google Chat, etc.)
    and maintain a single conversation session across all platforms.

    Admin can manage identity linking via Firestore console by editing
    the identities array.
    """
    id: Optional[str] = Field(
        default=None,
        description="Firestore document ID"
    )
    identities: list[PlatformIdentity] = Field(
        ...,
        description="List of platform identities for this user"
    )
    primary_name: str = Field(
        ...,
        description="Primary display name for this user"
    )
    email: Optional[str] = Field(
        default=None,
        description="Email address for auto-linking (especially for Google Chat)"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this user was created"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Last update timestamp"
    )

    model_config = {"frozen": False}  # Mutable for updates

    def get_platform_identity(self, platform: str) -> Optional[PlatformIdentity]:
        """
        Get user's identity on a specific platform.

        Args:
            platform: Platform name (e.g., "slack", "google_chat")

        Returns:
            PlatformIdentity if found, None otherwise
        """
        for identity in self.identities:
            if identity.platform == platform:
                return identity
        return None

    def has_platform(self, platform: str) -> bool:
        """
        Check if user has an identity on the given platform.

        Args:
            platform: Platform name (e.g., "slack", "google_chat")

        Returns:
            True if user has identity on this platform
        """
        return self.get_platform_identity(platform) is not None
