"""Base platform connector interface."""
from abc import ABC, abstractmethod
from typing import Optional
from fastapi import Request

from app.schemas.platform_event import PlatformEvent


class PlatformConnector(ABC):
    """
    Abstract base class for platform-specific connectors.

    Each messaging platform (Slack, Google Chat, etc.) implements this interface
    to provide a unified abstraction for sending messages, downloading files,
    and handling platform-specific authentication.
    """

    @abstractmethod
    async def send_message(self, recipient_id: str, text: str) -> dict:
        """
        Send a text message to a user.

        Args:
            recipient_id: Platform-specific identifier for the recipient
                         (e.g., Slack channel ID, Google Chat space name)
            text: Message text to send

        Returns:
            Platform-specific response dict

        Raises:
            Exception: If message sending fails
        """
        pass

    @abstractmethod
    async def download_file(self, download_ref: str) -> bytes:
        """
        Download a file using a platform-specific reference.

        The reference is opaque to the caller — each connector decides what
        it accepts (Slack: signed URL; Telegram: file_id; Google Chat:
        attachment resourceName) and produces matching values in the
        PlatformEvent.files dicts.

        Args:
            download_ref: Connector-specific reference to the file

        Returns:
            Raw file bytes

        Raises:
            FileDownloadError: If download fails
        """
        pass

    @abstractmethod
    async def get_user_info(self, user_id: str) -> dict:
        """
        Get user profile information.

        Args:
            user_id: Platform-specific user identifier

        Returns:
            Dict with user info (format varies by platform):
                - display_name: User's display name
                - email: Email address (if available)
                - real_name: User's real name (if available)

        Raises:
            Exception: If user lookup fails
        """
        pass

    @abstractmethod
    async def open_conversation(self, user_id: str) -> str:
        """
        Open or get a direct message conversation with a user.

        Args:
            user_id: Platform-specific user identifier

        Returns:
            Platform-specific conversation/channel/space identifier

        Raises:
            Exception: If conversation cannot be opened
        """
        pass

    @abstractmethod
    async def verify_request(self, request: Request) -> bool:
        """
        Verify that an incoming webhook request is authentic.

        Each platform has its own verification mechanism:
        - Slack: HMAC signature verification
        - Google Chat: Bearer token JWT verification

        Args:
            request: FastAPI request object

        Returns:
            True if request is verified, False otherwise
        """
        pass

    @abstractmethod
    def parse_event(self, data: dict) -> PlatformEvent:
        """
        Parse platform-specific event into unified PlatformEvent.

        Args:
            data: Raw platform event dict

        Returns:
            Normalized PlatformEvent

        Raises:
            Exception: If event cannot be parsed
        """
        pass
