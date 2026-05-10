# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Identity resolution service for multi-platform user management."""
import logging
from typing import Optional
from datetime import datetime

from app.models.user import User, PlatformIdentity

logger = logging.getLogger(__name__)


class IdentityService:
    """
    Manages user identity resolution across multiple platforms.

    This service handles:
    - Resolving platform-specific IDs to unified User objects
    - Auto-linking users via email (for Google Chat)
    - Creating new users on first message
    - Retrieving platform-specific IDs for sending messages
    """

    def __init__(self, firestore_service):
        """
        Initialize identity service.

        Args:
            firestore_service: FirestoreService instance for data access
        """
        self.firestore = firestore_service

    async def resolve_user(
        self,
        platform: str,
        platform_user_id: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None
    ) -> User:
        """
        Resolve a platform identity to a unified User.

        Resolution strategy:
        1. Look up user by platform identity
        2. If not found and email provided, try email-based lookup (auto-link)
        3. If not found, create new user

        Args:
            platform: Platform name (e.g., "slack", "google_chat")
            platform_user_id: Platform-specific user ID
            email: Optional email for auto-linking
            display_name: Optional display name from platform

        Returns:
            User object (existing or newly created)
        """
        # Step 1: Try direct platform identity lookup
        user = await self.firestore.get_user_by_identity(platform, platform_user_id)
        if user:
            logger.debug(f"Found existing user {user.id} for {platform}:{platform_user_id}")
            return user

        # Step 2: Email-based auto-linking (primarily for Google Chat)
        if email:
            user = await self.firestore.get_user_by_email(email)
            if user:
                # Auto-link this platform identity to existing user
                logger.info(
                    f"Auto-linking {platform}:{platform_user_id} to user {user.id} "
                    f"via email {email}"
                )
                await self.link_identity(
                    user_id=user.id,
                    platform=platform,
                    platform_user_id=platform_user_id,
                    display_name=display_name
                )
                # Reload user to get updated identities
                user = await self.firestore.get_user_by_id(user.id)
                return user

        # Step 3: Create new user (graceful degradation)
        logger.info(
            f"Creating new user for {platform}:{platform_user_id} "
            f"(email: {email or 'none'})"
        )
        user = User(
            primary_name=display_name or platform_user_id,
            email=email,
            identities=[
                PlatformIdentity(
                    platform=platform,
                    platform_user_id=platform_user_id,
                    display_name=display_name
                )
            ]
        )
        user_id = await self.firestore.create_user(user)
        user.id = user_id
        logger.info(f"Created new user {user_id}")
        return user

    async def get_platform_identity(
        self,
        user_id: str,
        platform: str
    ) -> Optional[str]:
        """
        Get user's platform-specific ID for a given platform.

        Used when sending messages - need to convert unified user_id
        back to platform-specific identifier.

        Args:
            user_id: Unified user ID
            platform: Platform name (e.g., "slack", "google_chat")

        Returns:
            Platform-specific user ID if user has identity on that platform,
            None otherwise
        """
        user = await self.firestore.get_user_by_id(user_id)
        if not user:
            logger.warning(f"User {user_id} not found")
            return None

        identity = user.get_platform_identity(platform)
        if not identity:
            logger.warning(f"User {user_id} has no identity on platform {platform}")
            return None

        return identity.platform_user_id

    async def link_identity(
        self,
        user_id: str,
        platform: str,
        platform_user_id: str,
        display_name: Optional[str] = None
    ) -> None:
        """
        Link a new platform identity to an existing user.

        This is called during auto-linking or can be done manually
        by admin via Firestore console.

        Args:
            user_id: Existing user ID
            platform: Platform to link
            platform_user_id: Platform-specific user ID
            display_name: Optional display name on platform
        """
        user = await self.firestore.get_user_by_id(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        # Check if identity already exists
        if user.has_platform(platform):
            existing = user.get_platform_identity(platform)
            if existing.platform_user_id == platform_user_id:
                logger.debug(f"Identity {platform}:{platform_user_id} already linked")
                return
            else:
                logger.warning(
                    f"User {user_id} already has different {platform} identity: "
                    f"{existing.platform_user_id} (trying to link {platform_user_id})"
                )
                raise ValueError(
                    f"User already has {platform} identity: {existing.platform_user_id}"
                )

        # Add new identity
        new_identity = PlatformIdentity(
            platform=platform,
            platform_user_id=platform_user_id,
            display_name=display_name
        )

        await self.firestore.add_user_identity(user_id, new_identity)
        logger.info(f"Linked {platform}:{platform_user_id} to user {user_id}")
