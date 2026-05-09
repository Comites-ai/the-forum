# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Slack platform connector implementation."""
import logging
import hmac
import hashlib
import time
from typing import Optional
from fastapi import Request

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from google.cloud import secretmanager
import aiohttp

from app.services.platforms.base import PlatformConnector
from app.schemas.platform_event import PlatformEvent
from app.core.exceptions import FileDownloadError

logger = logging.getLogger(__name__)


class SlackConnector(PlatformConnector):
    """
    Slack platform connector implementation.

    Handles Slack-specific operations including message sending,
    file downloads, and webhook verification.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        signing_secret: Optional[str] = None,
        bot_token_secret: Optional[str] = None,
        bot_token_project_id: Optional[str] = None
    ):
        """
        Initialize Slack connector.

        Args:
            bot_token: [DEPRECATED] Direct Slack Bot User OAuth Token (xoxb-...)
            signing_secret: Slack signing secret for webhook verification
            bot_token_secret: Secret Manager secret name for Slack bot token
            bot_token_project_id: GCP project ID where the secret is stored

        Note:
            Either bot_token OR (bot_token_secret + bot_token_project_id) must be provided.
            The Secret Manager approach is preferred for better security.
        """
        self.signing_secret = signing_secret

        # Fetch token from Secret Manager if configured
        if bot_token_secret and bot_token_project_id:
            self.bot_token = self._fetch_token_from_secret_manager(
                bot_token_secret, bot_token_project_id
            )
        elif bot_token:
            # Backward compatibility: use direct token
            self.bot_token = bot_token
        else:
            raise ValueError(
                "Either bot_token OR (bot_token_secret + bot_token_project_id) must be provided"
            )

        self.client = AsyncWebClient(token=self.bot_token)

    def _fetch_token_from_secret_manager(self, secret_name: str, project_id: str) -> str:
        """
        Fetch Slack bot token from Secret Manager.

        Args:
            secret_name: Secret Manager secret name
            project_id: GCP project ID where secret is stored

        Returns:
            Slack bot token

        Raises:
            Exception: If secret cannot be accessed
        """
        try:
            client = secretmanager.SecretManagerServiceClient()
            secret_path = f"projects/{project_id}/secrets/{secret_name}/versions/latest"

            response = client.access_secret_version(request={"name": secret_path})
            token = response.payload.data.decode('UTF-8').strip()

            logger.debug(f"Fetched Slack bot token from secret: {secret_name} in project: {project_id}")
            return token
        except Exception as e:
            logger.error(f"Failed to fetch Slack bot token from secret {secret_name} in project {project_id}: {e}")
            raise

    async def send_message(self, recipient_id: str, text: str) -> dict:
        """
        Send message to Slack channel.

        Args:
            recipient_id: Slack channel ID or DM channel ID
            text: Message text to send

        Returns:
            Slack API response dict

        Raises:
            SlackApiError: If Slack API call fails
        """
        try:
            logger.debug(f"Posting message to Slack channel: {recipient_id}")
            response = await self.client.chat_postMessage(
                channel=recipient_id,
                text=text
            )

            if response["ok"]:
                response_channel = response.get("channel")
                response_ts = response.get("ts")
                logger.info(
                    f"Successfully posted message to channel: {recipient_id}, "
                    f"response_channel: {response_channel}, ts: {response_ts}"
                )

                if response_channel != recipient_id:
                    logger.warning(
                        f"Channel mismatch! Requested: {recipient_id}, "
                        f"Got: {response_channel}"
                    )
            else:
                logger.error(
                    f"Slack API returned ok=False for channel {recipient_id}: "
                    f"{response}"
                )

            return response

        except SlackApiError as e:
            error_message = e.response.get("error", "unknown_error")
            logger.error(
                f"Slack API error posting to channel {recipient_id}: {error_message}"
            )
            raise
        except Exception as e:
            logger.error(
                f"Unexpected error posting to Slack channel {recipient_id}: {e}"
            )
            raise

    async def download_file(self, download_ref: str) -> bytes:
        """
        Download a file from Slack's private URL.

        Slack file URLs (url_private) require authentication with the bot token.

        Args:
            download_ref: The url_private from a Slack file object

        Returns:
            Raw file bytes

        Raises:
            FileDownloadError: If download fails (network error or non-200 status)
        """
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {self.bot_token}"}
                async with session.get(download_ref, headers=headers) as response:
                    if response.status == 200:
                        file_bytes = await response.read()
                        logger.info(
                            f"Downloaded file from Slack: {len(file_bytes)} bytes"
                        )
                        return file_bytes
                    error_text = await response.text()
                    logger.error(
                        f"Slack file download failed: {response.status} - {error_text}"
                    )
                    raise FileDownloadError(
                        f"Slack returned {response.status} when downloading file"
                    )
        except FileDownloadError:
            raise
        except Exception as e:
            logger.error(f"Slack file download network error: {e}")
            raise FileDownloadError(f"Network error downloading Slack file: {e}") from e

    async def get_user_info(self, user_id: str) -> dict:
        """
        Get Slack user profile info (display name, real name).

        Args:
            user_id: Slack user ID (U...)

        Returns:
            User info dict from Slack API with keys:
                - display_name: User's display name
                - real_name: User's real name
                - email: Email (if available)

        Raises:
            SlackApiError: If Slack API call fails
        """
        try:
            response = await self.client.users_info(user=user_id)
            if response["ok"]:
                user = response["user"]
                profile = user.get("profile", {})
                return {
                    "display_name": profile.get("display_name") or user.get("real_name") or user_id,
                    "real_name": user.get("real_name"),
                    "email": profile.get("email")
                }
            else:
                logger.error(f"Failed to get user info: {response}")
                return {"display_name": user_id}
        except SlackApiError as e:
            logger.error(
                f"Error getting user info for {user_id}: {e.response.get('error')}"
            )
            return {"display_name": user_id}

    async def open_conversation(self, user_id: str, space_id: str = None) -> str:
        """
        Open or get existing DM conversation with a user.

        This ensures we're using the canonical DM channel ID, which can
        help with message threading issues.

        Args:
            user_id: Slack user ID to open DM with
            space_id: Optional space/channel ID (for consistency with other connectors, ignored for Slack)

        Returns:
            Channel ID for the DM conversation

        Raises:
            SlackApiError: If conversation cannot be opened
        """
        try:
            response = await self.client.conversations_open(users=[user_id])
            if response["ok"]:
                channel_id = response["channel"]["id"]
                logger.debug(
                    f"Opened conversation with user {user_id}: {channel_id}"
                )
                return channel_id
            else:
                logger.error(f"Failed to open conversation: {response}")
                raise Exception(f"Failed to open conversation: {response}")
        except SlackApiError as e:
            logger.error(
                f"Error opening conversation: {e.response.get('error')}"
            )
            raise

    async def verify_request(self, request: Request) -> bool:
        """
        Verify Slack request signature.

        Slack signs all requests with an HMAC-SHA256 signature.

        Args:
            request: FastAPI request object

        Returns:
            True if signature is valid, False otherwise
        """
        if not self.signing_secret:
            logger.warning("No signing secret configured, skipping verification")
            return True

        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")

        # Prevent replay attacks (reject requests older than 5 minutes)
        try:
            if abs(time.time() - int(timestamp)) > 60 * 5:
                logger.warning("Request timestamp too old, possible replay attack")
                return False
        except (ValueError, TypeError):
            logger.warning("Invalid timestamp in request")
            return False

        # Get request body
        body = await request.body()
        body_str = body.decode("utf-8")

        # Verify signature
        sig_basestring = f"v0:{timestamp}:{body_str}"
        computed_signature = (
            "v0="
            + hmac.new(
                self.signing_secret.encode(),
                sig_basestring.encode(),
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(computed_signature, signature)

    def parse_event(self, data: dict) -> PlatformEvent:
        """
        Parse Slack event into unified PlatformEvent.

        Args:
            data: Slack event callback data

        Returns:
            Normalized PlatformEvent

        Raises:
            ValueError: If event format is invalid
        """
        event_data = data.get("event", {})

        user_id = event_data.get("user")
        channel_id = event_data.get("channel")
        message_text = event_data.get("text", "")
        raw_files = event_data.get("files", [])

        if not user_id or not channel_id:
            raise ValueError(f"Invalid Slack event: missing user or channel")

        # Normalize Slack file objects to canonical {mimetype, download_ref, ...}
        files = []
        for f in raw_files:
            files.append({
                "mimetype": f.get("mimetype", ""),
                "download_ref": f.get("url_private") or f.get("url", ""),
                "name": f.get("name"),
                "size": f.get("size"),
            })

        return PlatformEvent(
            platform="slack",
            user_id=user_id,
            user_email=None,  # Slack doesn't provide email in events
            message_text=message_text,
            space_id=channel_id,
            files=files,
            raw_event=data
        )
